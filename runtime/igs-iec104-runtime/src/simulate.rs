//! The simulated changes of the points of a station (task C3).
//!
//! A change is a function of the step number and the seed of the point, so two runs with the same
//! configuration give the same values. The motions are in `points::SimulationKind`. A measured value
//! is kept on its grid of the profile (a multiple of 2^-15 for a normalized value), so that every
//! value the simulator produces can be encoded.

use std::time::Duration;

use igs_iec104::clock;
use igs_iec104::server::ServerHandle;
use igs_iec104_codec::header::InformationObjectAddress;
use serde_json::Value;
use tokio::task::JoinHandle;

use crate::points::{self, Kind, PointSpec, Simulation, SimulationKind};

/// The grid of a normalized value: 2^-15.
const NORMALIZED_SCALE: f64 = 32_768.0;

/// The values of one simulated point, step by step.
#[derive(Clone, Debug)]
pub struct Simulator {
    kind: Kind,
    simulation: Simulation,
    step: u64,
    /// The state of a single or double point: on or off.
    on: bool,
    /// The state of a measured value.
    level: f64,
}

impl Simulator {
    /// The simulator of a point, or `None` when the point does not change by itself.
    pub fn new(spec: &PointSpec) -> Option<Self> {
        let simulation = spec.simulation.clone()?;
        let on = match &spec.value {
            Value::Bool(on) => *on,
            Value::String(state) => state == "on",
            _ => false,
        };
        let level = spec.value.as_f64().unwrap_or(0.0);
        Some(Self {
            kind: spec.kind,
            simulation,
            step: 0,
            on,
            level,
        })
    }

    /// The value at the next step, in the JSON form `points::value_of` reads.
    pub fn step_value(&mut self) -> Value {
        self.step += 1;
        match self.simulation.kind {
            SimulationKind::Toggle => {
                self.on = !self.on;
                self.toggled()
            }
            SimulationKind::Ramp => {
                let increment = self.increment();
                self.level = self.wrap(self.level + increment);
                self.measured()
            }
            SimulationKind::Walk => {
                let increment = self.increment();
                let up = splitmix64(self.simulation.seed ^ self.step) & 1 == 1;
                let moved = if up {
                    self.level + increment
                } else {
                    self.level - increment
                };
                self.level = self.clamp(moved);
                self.measured()
            }
        }
    }

    fn increment(&self) -> f64 {
        self.simulation.increment.unwrap_or(0.0)
    }

    fn toggled(&self) -> Value {
        match self.kind {
            Kind::Double => Value::String(if self.on { "on" } else { "off" }.to_owned()),
            _ => Value::Bool(self.on),
        }
    }

    fn measured(&self) -> Value {
        match self.kind {
            Kind::Scaled => Value::from(self.level as i64),
            _ => serde_json::Number::from_f64(self.level).map_or(Value::Null, Value::Number),
        }
    }

    /// Keeps a value in the range of its kind, on the grid of the kind.
    fn wrap(&self, value: f64) -> f64 {
        match self.kind {
            Kind::Normalized => quantize(((value + 1.0).rem_euclid(2.0)) - 1.0),
            Kind::Scaled => ((value + 32_768.0).rem_euclid(65_536.0)) - 32_768.0,
            _ => value,
        }
    }

    /// Keeps a value in the range of its kind, without wrapping (a walk stops at the bounds).
    fn clamp(&self, value: f64) -> f64 {
        match self.kind {
            Kind::Normalized => quantize(value.clamp(-1.0, 1.0 - 1.0 / NORMALIZED_SCALE)),
            Kind::Scaled => value.clamp(-32_768.0, 32_767.0).round(),
            _ => value,
        }
    }
}

/// Rounds a normalized value to its grid.
fn quantize(value: f64) -> f64 {
    (value * NORMALIZED_SCALE).round() / NORMALIZED_SCALE
}

/// SplitMix64: a small, well-known generator, enough to draw the signs of a walk from a seed.
fn splitmix64(mut state: u64) -> u64 {
    state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// Starts the simulation of a point on the station: at each period, the point takes its next value
/// and the change is queued for the connections. Returns `None` when the point does not change.
pub fn drive(handle: ServerHandle, spec: PointSpec) -> Option<JoinHandle<()>> {
    let mut simulator = Simulator::new(&spec)?;
    let address: InformationObjectAddress = points::address(spec.address).ok()?;
    let period = Duration::from_millis(spec.simulation.as_ref()?.period_ms);
    Some(tokio::spawn(async move {
        let mut ticker = tokio::time::interval(period);
        // The first tick is immediate: the first change comes one period after the start.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            let json = simulator.step_value();
            let Ok(value) = points::value_of(spec.kind, spec.address, &json) else {
                tracing::error!(
                    address = spec.address,
                    "a simulated value does not fit its point"
                );
                return;
            };
            let Ok(time) = clock::now_utc() else {
                continue;
            };
            if let Err(error) = handle.update(address, value, time) {
                tracing::error!(address = spec.address, %error, "a simulated change was not published");
            }
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::points::{CommandPolicy, PointSpec, Simulation, SimulationKind};
    use serde_json::json;

    fn point(kind: Kind, value: Value, simulation: Simulation) -> PointSpec {
        PointSpec {
            address: 1,
            kind,
            value,
            stamped: false,
            command: CommandPolicy::UnknownAddress,
            simulation: Some(simulation),
        }
    }

    fn walk(seed: u64) -> Simulation {
        Simulation {
            kind: SimulationKind::Walk,
            period_ms: 100,
            increment: Some(0.25),
            seed,
        }
    }

    fn run(spec: &PointSpec, steps: usize) -> Vec<Value> {
        let mut simulator = Simulator::new(spec).expect("a simulated point");
        (0..steps).map(|_| simulator.step_value()).collect()
    }

    #[test]
    fn a_toggle_changes_state_at_each_step() {
        let spec = point(
            Kind::Single,
            json!(false),
            Simulation {
                kind: SimulationKind::Toggle,
                period_ms: 100,
                increment: None,
                seed: 0,
            },
        );
        assert_eq!(
            run(&spec, 4),
            vec![json!(true), json!(false), json!(true), json!(false)]
        );
    }

    #[test]
    fn a_double_point_toggles_between_on_and_off() {
        let spec = point(
            Kind::Double,
            json!("off"),
            Simulation {
                kind: SimulationKind::Toggle,
                period_ms: 100,
                increment: None,
                seed: 0,
            },
        );
        assert_eq!(run(&spec, 2), vec![json!("on"), json!("off")]);
    }

    #[test]
    fn a_normalized_ramp_wraps_inside_its_range_on_the_grid() {
        let spec = point(
            Kind::Normalized,
            json!(0.5),
            Simulation {
                kind: SimulationKind::Ramp,
                period_ms: 100,
                increment: Some(0.25),
                seed: 0,
            },
        );
        let values: Vec<f64> = run(&spec, 4)
            .iter()
            .map(|value| value.as_f64().expect("a number"))
            .collect();
        // 0.5 + 0.25 = 0.75; 1.0 wraps to -1.0, as the range is [-1, 1).
        assert_eq!(values, vec![0.75, -1.0, -0.75, -0.5]);
        for value in run(&spec, 40) {
            let number = value.as_f64().expect("a number");
            assert!((-1.0..1.0).contains(&number));
            assert_eq!((number * NORMALIZED_SCALE).fract(), 0.0);
        }
    }

    #[test]
    fn a_scaled_ramp_wraps_around_the_16_bit_range() {
        let spec = point(
            Kind::Scaled,
            json!(32_000),
            Simulation {
                kind: SimulationKind::Ramp,
                period_ms: 100,
                increment: Some(1_000.0),
                seed: 0,
            },
        );
        // 33 000 wraps to -32 536, then -31 536.
        assert_eq!(run(&spec, 2), vec![json!(-32_536), json!(-31_536)]);
    }

    #[test]
    fn a_seeded_walk_repeats_its_values_and_follows_its_seed() {
        let first = point(Kind::Float, json!(0.0), walk(42));
        let again = point(Kind::Float, json!(0.0), walk(42));
        let other = point(Kind::Float, json!(0.0), walk(43));
        assert_eq!(
            run(&first, 50),
            run(&again, 50),
            "the same seed repeats the run"
        );
        assert_ne!(
            run(&first, 50),
            run(&other, 50),
            "another seed gives another run"
        );
    }

    #[test]
    fn a_walk_stays_inside_the_range_of_a_normalized_point() {
        let spec = point(
            Kind::Normalized,
            json!(0.0),
            Simulation {
                kind: SimulationKind::Walk,
                period_ms: 100,
                increment: Some(0.5),
                seed: 7,
            },
        );
        for value in run(&spec, 200) {
            let number = value.as_f64().expect("a number");
            assert!(
                (-1.0..1.0).contains(&number),
                "{number} is outside the range"
            );
        }
    }

    #[test]
    fn a_point_without_a_simulation_has_no_simulator() {
        let spec = PointSpec {
            address: 1,
            kind: Kind::Single,
            value: json!(true),
            stamped: false,
            command: CommandPolicy::UnknownAddress,
            simulation: None,
        };
        assert!(Simulator::new(&spec).is_none());
    }

    #[test]
    fn every_simulated_value_is_accepted_by_its_kind() {
        for (kind, value, simulation) in [
            (Kind::Scaled, json!(0), walk(9)),
            (Kind::Normalized, json!(0.0), walk(9)),
            (Kind::Float, json!(0.0), walk(9)),
        ] {
            let spec = point(kind, value, simulation);
            for json in run(&spec, 100) {
                points::value_of(kind, 1, &json).expect("the value fits its kind");
            }
        }
    }
}

#[cfg(test)]
mod driver_tests {
    use super::*;
    use crate::points::{CommandPolicy, PointSpec, Simulation, SimulationKind};
    use igs_iec104::server::{Handler, Operation, Refusal, Server, ServerConfig};
    use igs_iec104_codec::header::CommonAddress;
    use serde_json::json;

    /// A station that is never asked for anything: the driver only publishes values.
    struct Silent;

    impl Handler for Silent {
        fn select(&self, _: &Operation) -> Result<(), Refusal> {
            Err(Refusal::UnknownAddress)
        }

        fn execute(&self, _: &Operation) -> Result<(), Refusal> {
            Err(Refusal::UnknownAddress)
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_simulated_point_takes_the_value_of_each_step_at_its_period() {
        let server = Server::bind(
            ServerConfig::new(
                "127.0.0.1:0".parse().expect("address"),
                CommonAddress::new(1),
            ),
            Silent,
        )
        .await
        .expect("the station binds");
        let handle = server.handle();
        let spec = PointSpec {
            address: 4,
            kind: Kind::Scaled,
            value: json!(0),
            stamped: false,
            command: CommandPolicy::UnknownAddress,
            simulation: Some(Simulation {
                kind: SimulationKind::Ramp,
                period_ms: 100,
                increment: Some(50.0),
                seed: 0,
            }),
        };
        let address = points::address(spec.address).expect("an address");
        let initial = points::value_of(spec.kind, spec.address, &spec.value).expect("a value");
        handle
            .add_point(address, initial, false)
            .expect("the point is added");
        let task = drive(handle.clone(), spec.clone()).expect("a simulated point");

        let mut expected = Simulator::new(&spec).expect("a simulated point");
        for _ in 0..5 {
            // Just after the period: the driver has published the step, and nothing more.
            tokio::time::sleep(Duration::from_millis(101)).await;
            let json = expected.step_value();
            let want = points::value_of(spec.kind, spec.address, &json).expect("a value");
            assert_eq!(handle.value(address), Some(want));
        }
        task.abort();
    }
}

//! The points of a station, read from a configuration file (task S2).
//!
//! The file is JSON:
//!
//! ```json
//! {"schema_version": 1, "points": [{"address": 1, "kind": "single", "value": true, "stamped": false}]}
//! ```
//!
//! Each point has one kind, and its value is written in the form the kind table below gives.
//! A time-tagged point (`stamped`) reports its changes with their time. The address is an
//! information object address, 1 to 16777215, and each address is used once (IMPLEMENTATION.md §5).
//!
//! Two optional fields give the point its behaviour. `command` is how the station answers the
//! commands to the point: `unknown_address` (the default), `refuse`, or `accept` (only for a single or
//! double point). `simulation` makes the point change by itself: `{"kind": "toggle" | "ramp" | "walk",
//! "period_ms": N, "increment": X, "seed": S}`, with `toggle` for a single or double point, and `ramp` or
//! `walk` for a normalized, scaled, or float point (see `simulate`).

use std::fmt;
use std::path::Path;

use igs_iec104::process_image::PointValue;
use igs_iec104_codec::elements::{
    Bcr, Bsi, Diq, DoublePoint, Nva, Qds, QualityFlags, Scd, ShortFloat, Siq, Sva, Vti,
};
use igs_iec104_codec::header::InformationObjectAddress;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The version of the points file this runtime reads.
pub const SCHEMA_VERSION: u32 = 1;

/// The largest information object address (three octets).
const MAX_ADDRESS: u32 = 16_777_215;

/// The kind of a point: the monitoring type it is served as.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// Single-point information: M_SP_NA_1, or M_SP_TB_1 when stamped. Its value is a boolean.
    Single,
    /// Double-point information: M_DP_NA_1, or M_DP_TB_1 when stamped. Its value is one of
    /// `off`, `on`, `intermediate`, or `indeterminate`.
    Double,
    /// Normalized value: M_ME_NA_1, or M_ME_TD_1 when stamped. Its value is a number from -1 up to
    /// 1, exclusive of 1, on a grid of 2^-15.
    Normalized,
    /// Scaled value: M_ME_NB_1, or M_ME_TE_1 when stamped. Its value is an integer from -32768 to
    /// 32767.
    Scaled,
    /// Short floating point: M_ME_NC_1, or M_ME_TF_1 when stamped. Its value is a finite number.
    Float,
    /// Integrated total (a binary counter reading): M_IT_NA_1, or M_IT_TB_1 when stamped. Its
    /// value is an integer from -2147483648 to 2147483647.
    Counter,
    /// Step position: M_ST_NA_1, or M_ST_TB_1 when stamped. Its value is an object with `value`, an
    /// integer from -64 to 63, and `transient`, true or false.
    Step,
    /// Bitstring of 32 bits: M_BO_NA_1, or M_BO_TB_1 when stamped. Its value is an integer from 0 to
    /// 4294967295, where bit n (1 to 32) is the bit of 2^(n - 1).
    Bitstring,
    /// Packed single-point information with change detection: M_PS_NA_1, which has no time-tagged form.
    /// Its value is an object with `status` and `change`, each an integer from 0 to 65535, where bit n
    /// (1 to 16) is point n.
    Packed,
    /// Normalized value without quality: M_ME_ND_1, which has no time-tagged form. Its value is a number
    /// from -1 up to 1, exclusive of 1, on a grid of 2^-15.
    NormalizedUnqualified,
}

impl Kind {
    /// The value the kind takes, for a message.
    fn expected(self) -> &'static str {
        match self {
            Self::Single => "true or false",
            Self::Double => "\"off\", \"on\", \"intermediate\" or \"indeterminate\"",
            Self::Normalized => "a number from -1 up to 1, excluding 1",
            Self::Scaled => "an integer from -32768 to 32767",
            Self::Float => "a finite number",
            Self::Counter => "an integer from -2147483648 to 2147483647",
            Self::Step => "an object with 'value' from -64 to 63 and 'transient' true or false",
            Self::Bitstring => "an integer from 0 to 4294967295",
            Self::Packed => "an object with 'status' and 'change', each an integer from 0 to 65535",
            Self::NormalizedUnqualified => "a number from -1 up to 1, excluding 1",
        }
    }
}

/// One point of the station, as written in the configuration.
#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PointSpec {
    /// The information object address, 1 to 16777215.
    pub address: u32,
    /// The kind of the point.
    pub kind: Kind,
    /// The value, in the form of the kind.
    pub value: Value,
    /// Whether the point is time-tagged.
    #[serde(default)]
    pub stamped: bool,
    /// How the station answers the commands to the point (§7). Unknown address by default.
    #[serde(default)]
    pub command: CommandPolicy,
    /// How the point changes by itself, for a simulated station. A point that does not change has none.
    #[serde(default)]
    pub simulation: Option<Simulation>,
}

/// How the station answers the commands to a point (task C3, §7).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandPolicy {
    /// The point has no command: the station refuses it as an unknown address (cause 47). The default.
    #[default]
    UnknownAddress,
    /// The station confirms the command. The point keeps its value: the controller updates it.
    Accept,
    /// The station refuses the command with a negative confirmation.
    Refuse,
}

/// How a point changes by itself (task C3). Every change is a function of the step number and the
/// seed, so a run is repeatable.
#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Simulation {
    /// The motion of the point.
    pub kind: SimulationKind,
    /// The time between two steps, in milliseconds (at least 1).
    pub period_ms: u64,
    /// The change at each step: for `ramp` (any sign) and `walk` (its size, above zero). Not used by
    /// `toggle`. A scaled point takes an integer.
    #[serde(default)]
    pub increment: Option<f64>,
    /// The seed of `walk`: the sign of each step is drawn from it.
    #[serde(default)]
    pub seed: u64,
}

/// The motions of a simulated point.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SimulationKind {
    /// A single or double point changes state at each step.
    Toggle,
    /// A measured value moves by the increment at each step, and wraps around its range.
    Ramp,
    /// A measured value moves by the increment up or down at each step, drawn from the seed.
    Walk,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PointFile {
    schema_version: u32,
    points: Vec<PointSpec>,
}

/// A points file that the runtime cannot use.
#[derive(Debug)]
pub enum PointError {
    /// The file cannot be read.
    Read {
        /// The path of the file.
        path: String,
        /// The reason.
        error: std::io::Error,
    },
    /// The file is not a valid points file: a syntax error, or a field that is not known.
    Parse(String),
    /// The file has a schema version this runtime does not read.
    SchemaVersion(u32),
    /// An address outside 1 to 16777215.
    Address(u32),
    /// An address used by two points.
    Duplicate(u32),
    /// A command policy that the kind of the point cannot take.
    Command {
        /// The address of the point.
        address: u32,
        /// Why.
        reason: &'static str,
    },
    /// A simulation that the kind of the point or its parameters cannot take.
    Simulation {
        /// The address of the point.
        address: u32,
        /// Why.
        reason: &'static str,
    },
    /// A value that does not fit the kind of its point.
    Value {
        /// The address of the point.
        address: u32,
        /// The kind of the point.
        kind: Kind,
        /// The form the value must take.
        expected: &'static str,
    },
}

impl fmt::Display for PointError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read { path, error } => write!(f, "cannot read the points file {path}: {error}"),
            Self::Parse(reason) => write!(f, "the points file is not valid: {reason}"),
            Self::SchemaVersion(found) => write!(
                f,
                "the points file has schema_version {found}, this runtime reads {SCHEMA_VERSION}"
            ),
            Self::Address(address) => {
                write!(
                    f,
                    "address {address} is out of range: use 1 to {MAX_ADDRESS}"
                )
            }
            Self::Duplicate(address) => write!(f, "address {address} is used by two points"),
            Self::Command { address, reason } => write!(f, "point {address}: {reason}"),
            Self::Simulation { address, reason } => write!(f, "point {address}: {reason}"),
            Self::Value {
                address,
                kind,
                expected,
            } => write!(
                f,
                "point {address} is {kind:?}, so its value must be {expected}"
            ),
        }
    }
}

impl std::error::Error for PointError {}

/// Reads and checks the points of the file at `path`.
pub fn load(path: &Path) -> Result<Vec<PointSpec>, PointError> {
    let text = std::fs::read_to_string(path).map_err(|error| PointError::Read {
        path: path.display().to_string(),
        error,
    })?;
    parse(&text)
}

/// Reads and checks the points of a file's text: the schema version, the addresses, the
/// duplicates, and the value of each point.
pub fn parse(text: &str) -> Result<Vec<PointSpec>, PointError> {
    let file: PointFile =
        serde_json::from_str(text).map_err(|error| PointError::Parse(error.to_string()))?;
    if file.schema_version != SCHEMA_VERSION {
        return Err(PointError::SchemaVersion(file.schema_version));
    }
    let mut addresses = std::collections::BTreeSet::new();
    for point in &file.points {
        address(point.address)?;
        if !addresses.insert(point.address) {
            return Err(PointError::Duplicate(point.address));
        }
        point.point_value()?;
        check_command_and_simulation(point)?;
    }
    Ok(file.points)
}

/// Checks that the command policy and the simulation fit the kind of the point.
fn check_command_and_simulation(point: &PointSpec) -> Result<(), PointError> {
    let address = point.address;
    let single_or_double = matches!(point.kind, Kind::Single | Kind::Double);
    let measured = matches!(point.kind, Kind::Normalized | Kind::Scaled | Kind::Float);
    if point.command == CommandPolicy::Accept && !single_or_double {
        return Err(PointError::Command {
            address,
            reason: "a command can be accepted only on a single or double point",
        });
    }
    let Some(simulation) = &point.simulation else {
        return Ok(());
    };
    let simulation_error = |reason| PointError::Simulation { address, reason };
    if simulation.period_ms == 0 {
        return Err(simulation_error(
            "the period must be at least 1 millisecond",
        ));
    }
    match simulation.kind {
        SimulationKind::Toggle if !single_or_double => {
            Err(simulation_error("toggle needs a single or double point"))
        }
        SimulationKind::Ramp | SimulationKind::Walk if !measured => Err(simulation_error(
            "ramp and walk need a normalized, scaled, or float point",
        )),
        SimulationKind::Toggle => Ok(()),
        SimulationKind::Ramp | SimulationKind::Walk => {
            let increment = simulation
                .increment
                .filter(|increment| increment.is_finite())
                .ok_or_else(|| simulation_error("ramp and walk need a finite increment"))?;
            if simulation.kind == SimulationKind::Walk && increment <= 0.0 {
                return Err(simulation_error("the walk's increment must be above zero"));
            }
            if simulation.kind == SimulationKind::Ramp && increment == 0.0 {
                return Err(simulation_error("a ramp's increment must not be zero"));
            }
            if point.kind == Kind::Scaled && increment.fract() != 0.0 {
                return Err(simulation_error(
                    "a scaled point takes an integer increment",
                ));
            }
            Ok(())
        }
    }
}

/// The information object address for `address`, when it is in range.
pub fn address(address: u32) -> Result<InformationObjectAddress, PointError> {
    if address == 0 {
        return Err(PointError::Address(address));
    }
    InformationObjectAddress::new(address).ok_or(PointError::Address(address))
}

impl PointSpec {
    /// The value of the point, as the library stores it.
    pub fn point_value(&self) -> Result<PointValue, PointError> {
        value_of(self.kind, self.address, &self.value)
    }
}

/// The value of a point of `kind` at `address`, from its JSON form.
pub fn value_of(kind: Kind, address: u32, json: &Value) -> Result<PointValue, PointError> {
    let invalid = || PointError::Value {
        address,
        kind,
        expected: kind.expected(),
    };
    let quality = Qds::default();
    Ok(match kind {
        Kind::Single => PointValue::Single(Siq {
            on: json.as_bool().ok_or_else(invalid)?,
            quality: QualityFlags::default(),
        }),
        Kind::Double => PointValue::Double(Diq {
            state: double_from(json).ok_or_else(invalid)?,
            quality: QualityFlags::default(),
        }),
        Kind::Normalized => {
            let number = json.as_f64().ok_or_else(invalid)?;
            let value = Nva::try_from_f32(number as f32).ok_or_else(invalid)?;
            PointValue::Normalized(value, quality)
        }
        Kind::Scaled => {
            let number = json.as_i64().ok_or_else(invalid)?;
            let raw = i16::try_from(number).map_err(|_| invalid())?;
            PointValue::Scaled(Sva::new(raw), quality)
        }
        Kind::Float => {
            let number = json.as_f64().filter(|number| number.is_finite());
            let number = number.ok_or_else(invalid)?;
            PointValue::Float(ShortFloat::from_f32(number as f32), quality)
        }
        Kind::Counter => {
            let number = json.as_i64().ok_or_else(invalid)?;
            let counter = i32::try_from(number).map_err(|_| invalid())?;
            // The sequence number is the reading's own: a served counter starts at 0.
            PointValue::Counter(Bcr::new(counter, 0).ok_or_else(invalid)?)
        }
        Kind::Step => {
            let object = json.as_object().filter(|object| object.len() == 2);
            let object = object.ok_or_else(invalid)?;
            let value = object
                .get("value")
                .and_then(Value::as_i64)
                .ok_or_else(invalid)?;
            let value = i8::try_from(value).map_err(|_| invalid())?;
            let transient = object.get("transient").and_then(Value::as_bool);
            let transient = transient.ok_or_else(invalid)?;
            PointValue::Step(Vti::new(value, transient).ok_or_else(invalid)?, quality)
        }
        Kind::Bitstring => {
            let number = json.as_u64().ok_or_else(invalid)?;
            let number = u32::try_from(number).map_err(|_| invalid())?;
            PointValue::Bitstring(bitstring_from(number).ok_or_else(invalid)?, quality)
        }
        Kind::Packed => {
            let object = json.as_object().filter(|object| object.len() == 2);
            let object = object.ok_or_else(invalid)?;
            let status = object
                .get("status")
                .and_then(Value::as_u64)
                .ok_or_else(invalid)?;
            let change = object
                .get("change")
                .and_then(Value::as_u64)
                .ok_or_else(invalid)?;
            let status = u16::try_from(status).map_err(|_| invalid())?;
            let change = u16::try_from(change).map_err(|_| invalid())?;
            PointValue::Packed(packed_from(status, change).ok_or_else(invalid)?, quality)
        }
        Kind::NormalizedUnqualified => {
            let number = json.as_f64().ok_or_else(invalid)?;
            let value = Nva::try_from_f32(number as f32).ok_or_else(invalid)?;
            PointValue::NormalizedUnqualified(value)
        }
    })
}

/// The bitstring whose bit n (1 to 32) is bit n - 1 of `number`.
fn bitstring_from(number: u32) -> Option<Bsi> {
    (1..=32u8).try_fold(Bsi::new(), |bits, bit| {
        bits.with_bit(bit, number & (1u32 << (bit - 1)) != 0)
    })
}

/// The integer of a bitstring: bit n (1 to 32) is the bit of 2^(n - 1).
fn bitstring_to(bits: Bsi) -> u32 {
    (1..=32u8)
        .filter(|&bit| bits.bit(bit) == Some(true))
        .fold(0, |number, bit| number | (1u32 << (bit - 1)))
}

/// The packed information of `status` and `change`: bit n (1 to 16) is point n.
fn packed_from(status: u16, change: u16) -> Option<Scd> {
    (1..=16u8).try_fold(Scd::new(), |bits, point| {
        let mask = 1u16 << (point - 1);
        let bits = bits.with_status(point, status & mask != 0)?;
        bits.with_changed(point, change & mask != 0)
    })
}

/// The status and the change of packed information, each as its integer.
fn packed_to(bits: Scd) -> (u16, u16) {
    (1..=16u8).fold((0, 0), |(status, change), point| {
        let mask = 1u16 << (point - 1);
        let status = if bits.status(point) == Some(true) {
            status | mask
        } else {
            status
        };
        let change = if bits.changed(point) == Some(true) {
            change | mask
        } else {
            change
        };
        (status, change)
    })
}

fn double_from(json: &Value) -> Option<DoublePoint> {
    match json.as_str()? {
        "off" => Some(DoublePoint::Off),
        "on" => Some(DoublePoint::On),
        "intermediate" => Some(DoublePoint::IndeterminateOrIntermediate),
        "indeterminate" => Some(DoublePoint::Indeterminate),
        _ => None,
    }
}

/// The JSON form of a value, as the control API reports it.
pub fn json_of(value: PointValue) -> Value {
    match value {
        PointValue::Single(siq) => Value::Bool(siq.on),
        PointValue::Double(diq) => Value::String(
            match diq.state {
                DoublePoint::Off => "off",
                DoublePoint::On => "on",
                DoublePoint::IndeterminateOrIntermediate => "intermediate",
                DoublePoint::Indeterminate => "indeterminate",
            }
            .to_owned(),
        ),
        PointValue::Normalized(nva, _) => number(f64::from(nva.as_f32())),
        PointValue::Scaled(sva, _) => Value::from(sva.value()),
        PointValue::Float(float, _) => number(f64::from(float.as_f32())),
        PointValue::Counter(counter) => Value::from(counter.counter()),
        PointValue::Step(step, _) => serde_json::json!({
            "value": step.value(),
            "transient": step.is_transient(),
        }),
        PointValue::Bitstring(bits, _) => Value::from(bitstring_to(bits)),
        PointValue::Packed(bits, _) => {
            let (status, change) = packed_to(bits);
            serde_json::json!({"status": status, "change": change})
        }
        PointValue::NormalizedUnqualified(nva) => number(f64::from(nva.as_f32())),
    }
}

fn number(value: f64) -> Value {
    serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(points: &str) -> String {
        format!("{{\"schema_version\": 1, \"points\": [{points}]}}")
    }

    #[test]
    fn a_valid_file_gives_its_points() {
        let text = file(
            r#"{"address": 1, "kind": "single", "value": true},
               {"address": 2, "kind": "float", "value": 1.5, "stamped": true}"#,
        );
        let points = parse(&text).expect("valid");
        assert_eq!(points.len(), 2);
        assert!(!points[0].stamped);
        assert!(points[1].stamped);
    }

    #[test]
    fn an_unknown_field_is_refused() {
        let text = file(r#"{"address": 1, "kind": "single", "value": true, "group": 1}"#);
        assert!(matches!(parse(&text), Err(PointError::Parse(_))));
    }

    #[test]
    fn the_schema_version_must_be_the_one_read() {
        let text = r#"{"schema_version": 2, "points": []}"#;
        assert!(matches!(parse(text), Err(PointError::SchemaVersion(2))));
    }

    #[test]
    fn the_addresses_are_checked() {
        for address in [0, MAX_ADDRESS + 1] {
            let text = file(&format!(
                r#"{{"address": {address}, "kind": "single", "value": true}}"#
            ));
            assert!(matches!(parse(&text), Err(PointError::Address(_))));
        }
        let top = file(&format!(
            r#"{{"address": {MAX_ADDRESS}, "kind": "single", "value": true}}"#
        ));
        assert!(parse(&top).is_ok());
    }

    #[test]
    fn an_address_used_twice_is_refused() {
        let text = file(
            r#"{"address": 7, "kind": "single", "value": true},
               {"address": 7, "kind": "single", "value": false}"#,
        );
        assert!(matches!(parse(&text), Err(PointError::Duplicate(7))));
    }

    #[test]
    fn a_value_of_the_wrong_form_is_refused_with_its_point() {
        let cases = [
            ("single", "1"),
            ("double", "\"maybe\""),
            ("normalized", "1.0"),
            ("scaled", "40000"),
            ("float", "\"x\""),
        ];
        for (kind, value) in cases {
            let text = file(&format!(
                r#"{{"address": 3, "kind": "{kind}", "value": {value}}}"#
            ));
            assert!(
                matches!(parse(&text), Err(PointError::Value { address: 3, .. })),
                "{kind} with {value} must be refused"
            );
        }
    }

    #[test]
    fn every_kind_reads_back_as_the_json_it_was_given() {
        let cases = [
            (Kind::Single, Value::Bool(true)),
            (Kind::Double, Value::String("intermediate".to_owned())),
            (Kind::Normalized, Value::from(0.5)),
            (Kind::Scaled, Value::from(-700)),
            (Kind::Float, Value::from(12.5)),
        ];
        for (kind, json) in cases {
            let value = value_of(kind, 1, &json).expect("valid");
            assert_eq!(json_of(value), json, "{kind:?}");
        }
    }

    #[test]
    fn a_counter_point_takes_a_32_bit_integer_and_cannot_be_simulated_as_a_measure() {
        let points =
            parse(&file(r#"{"address": 1, "kind": "counter", "value": -5}"#)).expect("a counter");
        assert_eq!(points[0].kind, Kind::Counter);
        let value = points[0].point_value().expect("a value");
        assert_eq!(json_of(value), serde_json::json!(-5));
        for bad in [
            r#""value": 2147483648"#,
            r#""value": 1.5"#,
            r#""value": true"#,
        ] {
            let text = format!(r#"{{"address": 1, "kind": "counter", {bad}}}"#);
            assert!(parse(&file(&text)).is_err(), "{bad} is not a counter value");
        }
        let ramp = r#"{"address": 1, "kind": "counter", "value": 1,
            "simulation": {"kind": "ramp", "period_ms": 100, "increment": 1.0}}"#;
        assert!(parse(&file(ramp)).is_err(), "a ramp needs a measured point");
    }

    #[test]
    fn a_step_and_a_bitstring_take_their_forms_and_come_back_as_they_went() {
        let points = parse(&file(
            r#"{"address": 1, "kind": "step", "value": {"value": -64, "transient": true}},
               {"address": 2, "kind": "bitstring", "value": 2147483649}"#,
        ))
        .expect("the forms are accepted");
        let step = json_of(points[0].point_value().expect("a step"));
        assert_eq!(step, serde_json::json!({"value": -64, "transient": true}));
        let bits = json_of(points[1].point_value().expect("a bitstring"));
        assert_eq!(bits, serde_json::json!(2_147_483_649_u64));
        for u32_max in [0_u64, 1, 4_294_967_295] {
            let text = file(&format!(
                r#"{{"address": 1, "kind": "bitstring", "value": {u32_max}}}"#
            ));
            let point = &parse(&text).expect("a bitstring")[0];
            assert_eq!(
                json_of(point.point_value().expect("a value")),
                serde_json::json!(u32_max)
            );
        }
    }

    #[test]
    fn a_step_or_a_bitstring_outside_its_form_is_refused() {
        let bad = [
            r#"{"address": 1, "kind": "step", "value": {"value": 64, "transient": false}}"#,
            r#"{"address": 1, "kind": "step", "value": {"value": 1}}"#,
            r#"{"address": 1, "kind": "step", "value": {"value": 1, "transient": false, "x": 1}}"#,
            r#"{"address": 1, "kind": "step", "value": 1}"#,
            r#"{"address": 1, "kind": "bitstring", "value": 4294967296}"#,
            r#"{"address": 1, "kind": "bitstring", "value": -1}"#,
            r#"{"address": 1, "kind": "bitstring", "value": 1.5}"#,
        ];
        for text in bad {
            assert!(parse(&file(text)).is_err(), "{text} is not a valid form");
        }
    }

    #[test]
    fn a_step_and_a_bitstring_cannot_be_simulated() {
        for kind in ["step", "bitstring"] {
            let text = format!(
                r#"{{"address": 1, "kind": "{kind}", "value": {value},
                    "simulation": {{"kind": "walk", "period_ms": 100, "increment": 1.0}}}}"#,
                value = if kind == "step" {
                    r#"{"value": 0, "transient": false}"#
                } else {
                    "0"
                },
            );
            assert!(parse(&file(&text)).is_err(), "{kind} cannot be a walk");
        }
    }

    #[test]
    fn a_packed_and_an_unqualified_point_take_their_forms_and_come_back_as_they_went() {
        let points = parse(&file(
            r#"{"address": 1, "kind": "packed", "value": {"status": 65535, "change": 5}},
               {"address": 2, "kind": "normalized_unqualified", "value": -0.5}"#,
        ))
        .expect("the forms are accepted");
        let packed = json_of(points[0].point_value().expect("a packed point"));
        assert_eq!(packed, serde_json::json!({"status": 65535, "change": 5}));
        let unqualified = json_of(points[1].point_value().expect("an unqualified value"));
        assert_eq!(unqualified, serde_json::json!(-0.5));
    }

    #[test]
    fn a_packed_or_an_unqualified_value_outside_its_form_is_refused() {
        let bad = [
            r#"{"address": 1, "kind": "packed", "value": {"status": 65536, "change": 0}}"#,
            r#"{"address": 1, "kind": "packed", "value": {"status": 1}}"#,
            r#"{"address": 1, "kind": "packed", "value": {"status": -1, "change": 0}}"#,
            r#"{"address": 1, "kind": "packed", "value": 1}"#,
            r#"{"address": 1, "kind": "normalized_unqualified", "value": 1.0}"#,
            r#"{"address": 1, "kind": "normalized_unqualified", "value": "0.5"}"#,
        ];
        for text in bad {
            assert!(parse(&file(text)).is_err(), "{text} is not a valid form");
        }
        let walk = r#"{"address": 1, "kind": "normalized_unqualified", "value": 0.5,
            "simulation": {"kind": "walk", "period_ms": 100, "increment": 0.1}}"#;
        assert!(
            parse(&file(walk)).is_err(),
            "an unqualified value is not a measured point"
        );
    }
}

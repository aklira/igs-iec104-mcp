//! The server role (tasks S2 and C3): a station that serves the points of its configuration, answers
//! their commands by the configured policy, and reports its connections.
//!
//! A command is judged by the policy of its point: accepted, refused with a negative confirmation, or
//! refused as an unknown address (cause 47). Each judgement is queued for the event log without
//! waiting: the handler runs on a connection's task, so it must not block (see `Handler`).

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::net::SocketAddr;
use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use igs_iec104::process_image::ProcessError;
use igs_iec104::server::{
    CommandValue, Handler, Operation, Refusal, Server, ServerConfig, ServerError, ServerEvents,
    ServerHandle,
};
use igs_iec104_codec::header::CommonAddress;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::points::{self, CommandPolicy, PointError, PointSpec};

/// A command the station judged.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CommandRecord {
    /// The address of the command.
    pub address: u32,
    /// `single`, `double`, `step`, `normalized`, `scaled`, or `float`.
    pub command: &'static str,
    /// True for a select, false for an execution.
    pub select: bool,
    /// Whether the station confirmed the command.
    pub accepted: bool,
}

/// Judges the commands by the policy of their points, and queues each judgement without waiting.
pub struct Policy {
    policies: BTreeMap<u32, CommandPolicy>,
    commands: mpsc::Sender<CommandRecord>,
    lost: Arc<AtomicU64>,
}

impl Policy {
    fn judge(&self, operation: &Operation) -> Result<(), Refusal> {
        let address = operation.address.value();
        let verdict = match self.policies.get(&address).copied().unwrap_or_default() {
            CommandPolicy::UnknownAddress => Err(Refusal::UnknownAddress),
            CommandPolicy::Refuse => Err(Refusal::Rejected),
            CommandPolicy::Accept => Ok(()),
        };
        let record = CommandRecord {
            address,
            command: command_name(operation.value),
            select: operation.value.select(),
            accepted: verdict.is_ok(),
        };
        if self.commands.try_send(record).is_err() {
            self.lost.fetch_add(1, Ordering::Relaxed);
        }
        verdict
    }
}

impl Handler for Policy {
    fn select(&self, operation: &Operation) -> Result<(), Refusal> {
        self.judge(operation)
    }

    fn execute(&self, operation: &Operation) -> Result<(), Refusal> {
        self.judge(operation)
    }
}

fn command_name(value: CommandValue) -> &'static str {
    match value {
        CommandValue::Single(_) => "single",
        CommandValue::Double(_) => "double",
        CommandValue::Step(_) => "step",
        CommandValue::Normalized(..) => "normalized",
        CommandValue::Scaled(..) => "scaled",
        CommandValue::Float(..) => "float",
    }
}

/// A station that is bound and not yet running.
pub struct Station {
    /// The server, to run.
    pub server: Server<Policy>,
    /// The handle that updates the points while the station runs.
    pub handle: ServerHandle,
    /// The address the station listens on, with the port the runtime was given.
    pub listen: SocketAddr,
    /// The points, by address.
    pub points: BTreeMap<u32, PointSpec>,
    /// The connection events of the station.
    pub events: ServerEvents,
    /// The judgements of the commands, to be recorded.
    pub commands: mpsc::Receiver<CommandRecord>,
    /// The number of commands whose judgement could not be queued.
    pub lost_commands: Arc<AtomicU64>,
    /// The simulations of the points, which stop with the station.
    pub simulations: Vec<JoinHandle<()>>,
}

/// A station that cannot be started.
#[derive(Debug)]
pub enum StationError {
    /// The station did not bind its address.
    Bind(ServerError),
    /// The address of the station cannot be read.
    Address(io::Error),
    /// A point cannot be read.
    Point(PointError),
    /// The library refused a point.
    Register {
        /// The address of the point.
        address: u32,
        /// The reason.
        error: ProcessError,
    },
    /// The station's connection events were already taken.
    Events,
}

impl fmt::Display for StationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bind(error) => write!(f, "the station cannot start: {error}"),
            Self::Address(error) => write!(f, "the station's address cannot be read: {error}"),
            Self::Point(error) => write!(f, "{error}"),
            Self::Register { address, error } => {
                write!(f, "point {address} cannot be served: {error}")
            }
            Self::Events => write!(f, "the station's events are not available"),
        }
    }
}

impl std::error::Error for StationError {}

/// The settings of a station that are not in its points.
#[derive(Clone, Copy, Debug)]
pub struct StationSettings {
    /// The common address of the station.
    pub common_address: u16,
    /// The number of events kept for the stream of connection events, and for the command queue.
    pub event_capacity: NonZeroUsize,
    /// How long a selection waits for its execution.
    pub selection_timeout: Duration,
    /// The link parameters of the station's connections.
    pub parameters: igs_iec104_link::Parameters,
}

/// Binds the station on `listen`, and registers `points` before it runs. The commands are judged by
/// the policy of their points, which is known before the station binds.
pub async fn bind(
    listen: SocketAddr,
    settings: StationSettings,
    points: Vec<PointSpec>,
) -> Result<Station, StationError> {
    let policies = points
        .iter()
        .map(|point| (point.address, point.command))
        .collect();
    let (commands_tx, commands) = mpsc::channel(settings.event_capacity.get());
    let lost_commands = Arc::new(AtomicU64::new(0));
    let handler = Policy {
        policies,
        commands: commands_tx,
        lost: Arc::clone(&lost_commands),
    };
    let mut config = ServerConfig::new(listen, CommonAddress::new(settings.common_address))
        .with_events(settings.event_capacity);
    config.selection_timeout = settings.selection_timeout;
    config.parameters = settings.parameters;
    let mut server = Server::bind(config, handler)
        .await
        .map_err(StationError::Bind)?;
    let handle = server.handle();
    let listen = server.local_addr().map_err(StationError::Address)?;
    let events = server.events().ok_or(StationError::Events)?;
    let mut registered = BTreeMap::new();
    let mut simulations = Vec::new();
    for point in points {
        let address = points::address(point.address).map_err(StationError::Point)?;
        let value = point.point_value().map_err(StationError::Point)?;
        handle
            .add_point(address, value, point.stamped)
            .map_err(|error| StationError::Register {
                address: point.address,
                error,
            })?;
        if let Some(task) = crate::simulate::drive(handle.clone(), point.clone()) {
            simulations.push(task);
        }
        registered.insert(point.address, point);
    }
    Ok(Station {
        server,
        handle,
        listen,
        points: registered,
        events,
        commands,
        lost_commands,
        simulations,
    })
}

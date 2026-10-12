//! The runtime as a whole: the control listener, the role it runs, and the state they share.
//!
//! The control API has its own listener, separate from the IEC 60870-5-104 traffic (§3). It
//! binds the address it is given, and the runtime reports the addresses it got (§6).

use std::collections::BTreeMap;
use std::fmt;
use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use igs_iec104::server::{ServerEvent, ServerEvents, ServerHandle, ShutdownReport};
use igs_iec104::tls::events::{Discard, SecurityEvents};
use igs_iec104::tls::Secure;
use igs_iec104::Client;
use igs_iec104_codec::header::CommonAddress;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::client_role::{self, transfer_name_of, Recorder, Request};
use crate::config::{Config, Role, TlsFiles, DEFAULT_EVENT_CAPACITY};
use crate::points::{self, PointError, PointSpec};
use crate::server_role::{self, CommandRecord, Policy, StationError, StationSettings};
use crate::station_log::{StationEventKind, StationLog};
use crate::tls::StationSecurity;
use crate::tls::{self, TlsSetupError};
use crate::token::{ControlToken, TokenError};

/// Requests waiting for the client. A full queue makes a request wait for its turn.
const REQUEST_QUEUE: usize = 16;

/// How long a station waits for its connections to send what they hold, on a stop (task U1).
const STATION_GRACE: Duration = Duration::from_secs(5);

/// What the control API shares with the role.
pub struct Shared {
    /// The address the control API listens on.
    pub control: SocketAddr,
    /// The token every request of the control API must carry.
    pub token: ControlToken,
    /// The client, when the runtime runs as one.
    pub client: Option<ClientLink>,
    /// The station, when the runtime runs as one.
    pub server: Option<ServerLink>,
}

/// The control API's link to the client.
#[derive(Debug)]
pub struct ClientLink {
    /// The events and the status of the client.
    pub recorder: Arc<Recorder>,
    /// The requests to the client.
    pub requests: mpsc::Sender<Request>,
}

/// The control API's link to the station.
pub struct ServerLink {
    /// The handle that reads and updates the points.
    pub handle: ServerHandle,
    /// The address the station listens on.
    pub listen: SocketAddr,
    /// The points, by address, as they were configured.
    pub points: BTreeMap<u32, PointSpec>,
    /// The events of the station: its connections, and the commands it judged.
    pub log: Arc<Mutex<StationLog>>,
}

/// What a station's serving reports when it has stopped.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stopped {
    /// What the station did when it stopped: the connections it had, those it closed by force, and
    /// the events it still held (task U1). `None` for a client.
    pub station: Option<ShutdownReport>,
}

/// The parts of a station that run beside it: its connection events, the commands it judged, and its
/// simulations.
struct StationParts {
    server: igs_iec104::server::Server<Policy>,
    events: ServerEvents,
    commands: mpsc::Receiver<CommandRecord>,
    lost_commands: Arc<AtomicU64>,
    simulations: Vec<JoinHandle<()>>,
}

/// What the role runs, until the runtime serves.
enum Engine {
    Client {
        client: Client,
        requests: mpsc::Receiver<Request>,
        recorder: Arc<Recorder>,
        common_address: CommonAddress,
    },
    Server {
        parts: StationParts,
        log: Arc<Mutex<StationLog>>,
    },
}

/// A runtime that is bound and not yet serving. Binding first lets the caller learn the
/// addresses before the runtime serves.
pub struct Runtime {
    control: TcpListener,
    engine: Engine,
    shared: Arc<Shared>,
}

/// A runtime that cannot start.
#[derive(Debug)]
pub enum BindError {
    /// An address could not be bound.
    Io(io::Error),
    /// The points file cannot be used.
    Points(PointError),
    /// The station cannot start.
    Station(StationError),
    /// The control token cannot be used.
    Token(TokenError),
    /// The link parameters are not valid for the role.
    Link(igs_iec104_link::ConfigError),
    /// The TLS files cannot secure the connections.
    Tls(TlsSetupError),
}

impl fmt::Display for BindError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "an address could not be bound: {error}"),
            Self::Link(error) => write!(f, "the link parameters are not valid: {error}"),
            Self::Points(error) => write!(f, "{error}"),
            Self::Station(error) => write!(f, "{error}"),
            Self::Token(error) => write!(f, "{error}"),
            Self::Tls(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for BindError {}

impl Runtime {
    /// Binds the control address and starts the role. A client connects in the background.
    pub async fn bind(config: Config) -> Result<Self, BindError> {
        let token = ControlToken::read(&config.control_token).map_err(BindError::Token)?;
        let control = TcpListener::bind(config.control)
            .await
            .map_err(BindError::Io)?;
        let control_address = control.local_addr().map_err(BindError::Io)?;
        let (engine, client, server) = match config.role {
            Role::Client { connect } => {
                let secure = secured(config.tls.as_ref(), Arc::new(Discard))?;
                // A client checks the station's certificate against the server name, or against
                // the address it connects to.
                let host = config
                    .tls
                    .as_ref()
                    .and_then(|files| files.server_name.clone())
                    .unwrap_or_else(|| connect.ip().to_string());
                let client_config = client_role::client_config(
                    connect,
                    config.common_address,
                    config.parameters,
                    secure.map(|secure| (secure, host)),
                )
                .map_err(BindError::Link)?;
                let client = Client::connect(client_config);
                let (requests_tx, requests) = mpsc::channel(REQUEST_QUEUE);
                let recorder = Arc::new(Recorder::new(config.event_capacity));
                let link = ClientLink {
                    recorder: Arc::clone(&recorder),
                    requests: requests_tx,
                };
                let engine = Engine::Client {
                    client,
                    requests,
                    recorder,
                    common_address: CommonAddress::new(config.common_address),
                };
                (engine, Some(link), None)
            }
            Role::Server {
                listen,
                points,
                selection_timeout,
            } => {
                let points = points::load(&points).map_err(BindError::Points)?;
                // The log exists before the station binds: the handshakes record into it, so a
                // refused client leaves its reason there. The log keeps at least the default, so
                // that a small stream queue loses events before the log does, and the loss is
                // what the stream counted.
                let log = Arc::new(Mutex::new(StationLog::new(
                    config.event_capacity.max(DEFAULT_EVENT_CAPACITY),
                )));
                let secure = secured(
                    config.tls.as_ref(),
                    Arc::new(StationSecurity::new(Arc::clone(&log))),
                )?;
                let settings = StationSettings {
                    common_address: config.common_address,
                    event_capacity: NonZeroUsize::new(config.event_capacity)
                        .unwrap_or(NonZeroUsize::MIN),
                    selection_timeout,
                    parameters: config.parameters,
                };
                let station = server_role::bind(listen, settings, points, secure)
                    .await
                    .map_err(BindError::Station)?;
                let link = ServerLink {
                    handle: station.handle,
                    listen: station.listen,
                    points: station.points,
                    log: Arc::clone(&log),
                };
                let parts = StationParts {
                    server: station.server,
                    events: station.events,
                    commands: station.commands,
                    lost_commands: station.lost_commands,
                    simulations: station.simulations,
                };
                (Engine::Server { parts, log }, None, Some(link))
            }
        };
        Ok(Self {
            control,
            engine,
            shared: Arc::new(Shared {
                control: control_address,
                token,
                client,
                server,
            }),
        })
    }

    /// The address the control API listens on.
    pub fn control_address(&self) -> SocketAddr {
        self.shared.control
    }

    /// The address the station listens on, when the runtime runs as a station.
    pub fn station_address(&self) -> Option<SocketAddr> {
        self.shared.server.as_ref().map(|server| server.listen)
    }

    /// Serves the control API and runs the role until `shutdown` completes. The role stops
    /// with the control API: a station stops in order (task U1), and a client is dropped. The
    /// report of the stop comes back with the result.
    pub async fn serve<F>(self, shutdown: F) -> io::Result<Stopped>
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let Self {
            control,
            engine,
            shared,
        } = self;
        match engine {
            Engine::Client {
                client,
                requests,
                recorder,
                common_address,
            } => {
                // The owner holds only the recorder, not the request sender: it ends when the
                // control API ends, because the last sender is then dropped. It is aborted
                // after the control API, so that it cannot outlive the runtime.
                let owner =
                    tokio::spawn(client_role::run(client, requests, recorder, common_address));
                let served = axum::serve(control, crate::api::router(shared))
                    .with_graceful_shutdown(shutdown)
                    .await;
                owner.abort();
                served.map(|()| Stopped::default())
            }
            Engine::Server { parts, log } => {
                let StationParts {
                    server,
                    events,
                    commands,
                    lost_commands,
                    simulations,
                } = parts;
                let (stop, stopped) = oneshot::channel::<()>();
                let station = tokio::spawn(server.run_until(
                    async {
                        let _ = stopped.await;
                    },
                    STATION_GRACE,
                ));
                let recording = tokio::spawn(record_station(events, commands, lost_commands, log));
                let served = axum::serve(control, crate::api::router(shared))
                    .with_graceful_shutdown(shutdown)
                    .await;
                let _ = stop.send(());
                let report = match station.await {
                    Ok(report) => {
                        tracing::info!(?report, "the station stopped");
                        Some(report)
                    }
                    Err(error) => {
                        tracing::error!(%error, "the station task failed");
                        None
                    }
                };
                recording.abort();
                for simulation in simulations {
                    simulation.abort();
                }
                served.map(|()| Stopped { station: report })
            }
        }
    }
}

/// Records the connection events of the station and the commands it judged, in its log, and keeps
/// the count of the events it lost. It runs until it is aborted with the station.
async fn record_station(
    mut events: ServerEvents,
    mut commands: mpsc::Receiver<CommandRecord>,
    lost_commands: Arc<AtomicU64>,
    log: Arc<Mutex<StationLog>>,
) {
    let mut sync = tokio::time::interval(Duration::from_millis(50));
    loop {
        tokio::select! {
            Some(event) = events.next() => {
                crate::lock(&log).record(station_kind(&event));
            }
            Some(record) = commands.recv() => {
                crate::lock(&log).record(StationEventKind::Command {
                    address: record.address,
                    command: record.command,
                    select: record.select,
                    accepted: record.accepted,
                });
            }
            _ = sync.tick() => {
                let lost = events.dropped() + lost_commands.load(Ordering::Relaxed);
                crate::lock(&log).set_lost(lost);
            }
        }
    }
}

/// The log's view of a connection event of the station.
fn station_kind(event: &ServerEvent) -> StationEventKind {
    match event {
        ServerEvent::Connected { id, peer } => StationEventKind::Connected {
            connection: *id,
            peer: peer.to_string(),
        },
        ServerEvent::Transfer { id, state } => StationEventKind::Transfer {
            connection: *id,
            state: transfer_name_of(*state),
        },
        ServerEvent::Superseded { id, by } => StationEventKind::Superseded {
            connection: *id,
            by: *by,
        },
        ServerEvent::Closed { id, end } => {
            let (end, error) = match end {
                igs_iec104::server::ConnectionEnd::Station => ("station", None),
                igs_iec104::server::ConnectionEnd::Transport(error) => {
                    ("transport", Some(error.to_string()))
                }
                igs_iec104::server::ConnectionEnd::Forced => ("forced", None),
            };
            StationEventKind::Closed {
                connection: *id,
                end,
                error,
            }
        }
    }
}

/// The secure wrapper of a role's connections, when the configuration names TLS files. The
/// handshakes raise their security events to `events`.
fn secured(
    tls: Option<&TlsFiles>,
    events: Arc<dyn SecurityEvents>,
) -> Result<Option<Secure>, BindError> {
    tls.map(|files| tls::secure(files, events))
        .transpose()
        .map_err(BindError::Tls)
}

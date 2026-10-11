//! The runtime as a whole: the control listener, the role it runs, and the state they share.
//!
//! The control API has its own listener, separate from the IEC 60870-5-104 traffic (§3). It
//! binds the address it is given, and the runtime reports the addresses it got (§6).

use std::collections::BTreeMap;
use std::fmt;
use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use igs_iec104::server::{Server, ServerHandle};
use igs_iec104::Client;
use igs_iec104_codec::header::CommonAddress;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};

use crate::client_role::{self, Recorder, Request};
use crate::config::{Config, Role};
use crate::points::{self, PointError, PointSpec};
use crate::server_role::{self, Refuse, StationError};
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
        server: Server<Refuse>,
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
}

impl fmt::Display for BindError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "an address could not be bound: {error}"),
            Self::Points(error) => write!(f, "{error}"),
            Self::Station(error) => write!(f, "{error}"),
            Self::Token(error) => write!(f, "{error}"),
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
                let client =
                    Client::connect(client_role::client_config(connect, config.common_address));
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
            Role::Server { listen, points } => {
                let points = points::load(&points).map_err(BindError::Points)?;
                let station = server_role::bind(listen, config.common_address, points)
                    .await
                    .map_err(BindError::Station)?;
                let link = ServerLink {
                    handle: station.handle,
                    listen: station.listen,
                    points: station.points,
                };
                let engine = Engine::Server {
                    server: station.server,
                };
                (engine, None, Some(link))
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
    /// with the control API: a station stops in order (task U1), and a client is dropped.
    pub async fn serve<F>(self, shutdown: F) -> io::Result<()>
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
                served
            }
            Engine::Server { server } => {
                let (stop, stopped) = oneshot::channel::<()>();
                let station = tokio::spawn(server.run_until(
                    async {
                        let _ = stopped.await;
                    },
                    STATION_GRACE,
                ));
                let served = axum::serve(control, crate::api::router(shared))
                    .with_graceful_shutdown(shutdown)
                    .await;
                let _ = stop.send(());
                match station.await {
                    Ok(report) => tracing::info!(?report, "the station stopped"),
                    Err(error) => tracing::error!(%error, "the station task failed"),
                }
                served
            }
        }
    }
}

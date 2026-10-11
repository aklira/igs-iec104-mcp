//! The server role (task S2): a station that serves the points of its configuration.
//!
//! Commands are refused as unknown addresses (cause 47) for now. The configured command policy
//! (accept, refuse, or refuse as unknown address, per point or per command type) is task C3.

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::net::SocketAddr;

use igs_iec104::process_image::ProcessError;
use igs_iec104::server::{
    Handler, Operation, Refusal, Server, ServerConfig, ServerError, ServerHandle,
};
use igs_iec104_codec::header::CommonAddress;

use crate::points::{self, PointError, PointSpec};

/// Refuses every command: the station serves its points and nothing else.
pub struct Refuse;

impl Handler for Refuse {
    fn select(&self, _operation: &Operation) -> Result<(), Refusal> {
        Err(Refusal::UnknownAddress)
    }

    fn execute(&self, _operation: &Operation) -> Result<(), Refusal> {
        Err(Refusal::UnknownAddress)
    }
}

/// A station that is bound and not yet running.
pub struct Station {
    /// The server, to run.
    pub server: Server<Refuse>,
    /// The handle that updates the points while the station runs.
    pub handle: ServerHandle,
    /// The address the station listens on, with the port the runtime was given.
    pub listen: SocketAddr,
    /// The points, by address.
    pub points: BTreeMap<u32, PointSpec>,
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
        }
    }
}

impl std::error::Error for StationError {}

/// Binds the station on `listen` with the common address `common_address`, and registers
/// `points` before the station runs.
pub async fn bind(
    listen: SocketAddr,
    common_address: u16,
    points: Vec<PointSpec>,
) -> Result<Station, StationError> {
    let server = Server::bind(
        ServerConfig::new(listen, CommonAddress::new(common_address)),
        Refuse,
    )
    .await
    .map_err(StationError::Bind)?;
    let handle = server.handle();
    let listen = server.local_addr().map_err(StationError::Address)?;
    let mut registered = BTreeMap::new();
    for point in points {
        let address = points::address(point.address).map_err(StationError::Point)?;
        let value = point.point_value().map_err(StationError::Point)?;
        handle
            .add_point(address, value, point.stamped)
            .map_err(|error| StationError::Register {
                address: point.address,
                error,
            })?;
        registered.insert(point.address, point);
    }
    Ok(Station {
        server,
        handle,
        listen,
        points: registered,
    })
}

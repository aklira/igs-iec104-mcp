//! The owner of the client: the one task that reads its events and makes its requests.
//!
//! The client is configured to discard requests made without a connection
//! (`OfflineRequests::Discard`), as IMPLEMENTATION.md §7 requires: a request is never sent on a
//! connection the controller did not see open.

use std::sync::{Arc, Mutex};

use igs_iec104::{Client, ClientConfig, Delivery, Event, OfflineRequests};
use igs_iec104_codec::elements::Qoi;
use igs_iec104_codec::header::CommonAddress;
use igs_iec104_link::TransferState;
use serde::Serialize;
use tokio::sync::{mpsc, oneshot};

use crate::events::{AsduView, EventKind, EventLog};
use crate::lock;

/// The state of the connection and of the data transfer, as the control API reports it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Status {
    /// `down` until the connection is established, then `up`.
    pub connection: &'static str,
    /// The state of the data transfer, as in [`EventKind::Transfer`]. `stopped` while the
    /// connection is down.
    pub transfer: &'static str,
}

impl Default for Status {
    fn default() -> Self {
        Self {
            connection: "down",
            transfer: "stopped",
        }
    }
}

/// The state written by the owner and read by the control API.
#[derive(Debug)]
pub struct Recorder {
    /// The events of the client.
    pub log: Mutex<EventLog>,
    /// The connection and transfer state.
    pub status: Mutex<Status>,
}

impl Recorder {
    /// A recorder that keeps `capacity` events.
    pub fn new(capacity: usize) -> Self {
        Self {
            log: Mutex::new(EventLog::new(capacity)),
            status: Mutex::new(Status::default()),
        }
    }
}

/// A request of the control API to the client. It is answered when the client has queued it;
/// the outcome on the connection comes as events.
#[derive(Debug)]
pub enum Request {
    /// Start the data transfer.
    StartTransfer(oneshot::Sender<Result<(), String>>),
    /// A general interrogation of the station.
    GeneralInterrogation(oneshot::Sender<Result<(), String>>),
}

/// The configuration of the client of the runtime for the station at `connect`.
pub fn client_config(connect: std::net::SocketAddr, common_address: u16) -> ClientConfig {
    ClientConfig::new(connect, CommonAddress::new(common_address))
        .with_offline_requests(OfflineRequests::Discard)
}

/// Runs the client until its requests end or the client stops.
pub async fn run(
    mut client: Client,
    mut requests: mpsc::Receiver<Request>,
    recorder: Arc<Recorder>,
) {
    loop {
        tokio::select! {
            event = client.next_event() => match event {
                Some(event) => record(&recorder, &event),
                None => break,
            },
            request = requests.recv() => match request {
                Some(request) => answer(&client, request).await,
                None => break,
            },
        }
    }
}

/// Makes the request of the control API and sends back its acceptance.
async fn answer(client: &Client, request: Request) {
    match request {
        Request::StartTransfer(reply) => {
            let result = client.start_data_transfer().await;
            let _ = reply.send(result.map_err(|error| error.to_string()));
        }
        Request::GeneralInterrogation(reply) => {
            let result = client.interrogate(Qoi::STATION).await;
            let _ = reply.send(result.map_err(|error| error.to_string()));
        }
    }
}

/// Records an event of the client and updates the status it changes.
fn record(recorder: &Recorder, event: &Event) {
    let kind = kind_of(event);
    {
        let mut status = lock(&recorder.status);
        update(&mut status, &kind);
    }
    lock(&recorder.log).record(kind);
}

fn update(status: &mut Status, kind: &EventKind) {
    match kind {
        EventKind::Connected => status.connection = "up",
        EventKind::Disconnected { .. } | EventKind::ConnectFailed { .. } => {
            status.connection = "down";
            status.transfer = "stopped";
        }
        EventKind::Transfer { state } => status.transfer = state,
        _ => {}
    }
}

/// The control API's view of an event of the client.
pub fn kind_of(event: &Event) -> EventKind {
    match event {
        Event::Connected => EventKind::Connected,
        Event::Disconnected(error) => EventKind::Disconnected {
            error: error.to_string(),
        },
        Event::ConnectFailed(error) => EventKind::ConnectFailed {
            error: error.to_string(),
        },
        Event::Discarded(command) => EventKind::Discarded {
            command: format!("{command:?}"),
        },
        Event::Delivery(delivery) => delivery_kind(delivery),
        Event::Closed => EventKind::Closed,
    }
}

fn delivery_kind(delivery: &Delivery) -> EventKind {
    match delivery {
        Delivery::Asdu(asdu) => EventKind::Asdu {
            asdu: AsduView::of(asdu),
        },
        Delivery::Transmitted(asdu) => EventKind::Transmitted {
            asdu: AsduView::of(asdu),
        },
        Delivery::Rejected(rejection) => EventKind::Rejected {
            detail: format!("{rejection:?}"),
        },
        Delivery::Transfer(state) => EventKind::Transfer {
            state: transfer_name(*state),
        },
        Delivery::Unacknowledged(asdus) => EventKind::Unacknowledged {
            asdus: asdus.iter().map(AsduView::of).collect(),
        },
    }
}

/// The name of a state of the data transfer, as the control API reports it.
pub fn transfer_name(state: TransferState) -> &'static str {
    match state {
        TransferState::Stopped => "stopped",
        TransferState::PendingStarted => "pending_started",
        TransferState::Started => "started",
        TransferState::PendingStopped => "pending_stopped",
        TransferState::PendingUnconfirmedStop => "pending_unconfirmed_stop",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_client_is_down_and_stopped() {
        assert_eq!(Status::default().connection, "down");
        assert_eq!(Status::default().transfer, "stopped");
    }

    #[test]
    fn the_connection_and_the_transfer_follow_their_events() {
        let mut status = Status::default();
        update(&mut status, &EventKind::Connected);
        assert_eq!(status.connection, "up");
        update(&mut status, &EventKind::Transfer { state: "started" });
        assert_eq!(status.transfer, "started");
        update(
            &mut status,
            &EventKind::Disconnected {
                error: "lost".to_owned(),
            },
        );
        assert_eq!(status, Status::default());
    }

    #[test]
    fn a_transfer_delivery_is_named_by_its_state() {
        let kind = kind_of(&Event::Delivery(Delivery::Transfer(TransferState::Started)));
        assert_eq!(kind, EventKind::Transfer { state: "started" });
    }
}

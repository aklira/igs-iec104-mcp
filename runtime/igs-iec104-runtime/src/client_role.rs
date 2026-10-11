//! The owner of the client: the one task that reads its events, makes its requests, and records what
//! happens to them.
//!
//! The client is configured to discard requests made without a connection
//! (`OfflineRequests::Discard`), as IMPLEMENTATION.md §7 requires: a request is never sent on a
//! connection the controller did not see open. Its requests are judged by the correlator (see
//! `correlation`), and the points it receives are kept in the point image (see `image`).

use std::fmt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use igs_iec104::procedures;
use igs_iec104::{Client, ClientConfig, Delivery, Event, OfflineRequests};
use igs_iec104_codec::asdu::Asdu;
use igs_iec104_codec::elements::{CounterFreeze, Qcc, Qoc, Qoi, Sco};
use igs_iec104_codec::header::{CommonAddress, InformationObjectAddress};
use igs_iec104_link::TransferState;
use igs_iec104_link::{ConfigError as LinkError, Parameters};
use serde::Serialize;
use tokio::sync::{mpsc, oneshot};
use tokio::time::Instant as TokioInstant;

use crate::correlation::{
    Correlator, Finished, Kind, Outcome, RequestId, SubmitError, MAX_IN_FLIGHT,
};
use crate::events::{now_ms, AsduView, EventKind, EventLog};
use crate::image::PointImage;
use crate::lock;

/// The capacity of the client's channels. Above `MAX_IN_FLIGHT`, so that a request never waits for
/// room in the command channel (see the correlator).
pub const CLIENT_CAPACITY: usize = 32;
const _: () = assert!(MAX_IN_FLIGHT < CLIENT_CAPACITY);

/// The longest an interrogation may take to end, from its request.
pub const INTERROGATION_TIMEOUT: Duration = Duration::from_secs(30);

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
    /// The requests that are open, and those that ended recently.
    pub correlation: Mutex<Correlator>,
    /// The last value of each point the station sent.
    pub image: Mutex<PointImage>,
}

impl Recorder {
    /// A recorder that keeps `capacity` events.
    pub fn new(capacity: usize) -> Self {
        Self {
            log: Mutex::new(EventLog::new(capacity)),
            status: Mutex::new(Status::default()),
            correlation: Mutex::new(Correlator::new()),
            image: Mutex::new(PointImage::default()),
        }
    }
}

/// Why a request was not made.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The data transfer is not started: a command needs it (§7).
    NotStarted,
    /// Another open request has the same type and address.
    Busy(RequestId),
    /// The maximum of open requests is reached.
    TooManyOpen,
    /// The client has stopped.
    Stopped,
    /// The request cannot be encoded, with the reason.
    Invalid(String),
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotStarted => write!(f, "the data transfer is not started"),
            Self::Busy(id) => write!(f, "request {id} is still open for this address"),
            Self::TooManyOpen => write!(f, "too many requests are open"),
            Self::Stopped => write!(f, "the client has stopped"),
            Self::Invalid(reason) => write!(f, "the request cannot be encoded: {reason}"),
        }
    }
}

/// A request of the control API to the client. It is answered when the client has queued it; its
/// outcome comes as events.
#[derive(Debug)]
pub enum Request {
    /// Start the data transfer.
    StartTransfer(oneshot::Sender<Result<(), Refusal>>),
    /// A general interrogation of the station.
    Interrogate(oneshot::Sender<Result<RequestId, Refusal>>),
    /// A counter interrogation: the general request (`group` 0), or a group of counters (1 to 4).
    CounterInterrogate {
        /// 0 for every counter, 1 to 4 for a group.
        group: u8,
        /// Answers with the identity of the request, or why it was refused.
        reply: oneshot::Sender<Result<RequestId, Refusal>>,
    },
    /// A single command to a point.
    SingleCommand {
        /// The information object address of the command.
        address: InformationObjectAddress,
        /// The state the command asks for.
        on: bool,
        /// True for a select, false for an execution.
        select: bool,
        /// How long the request may stay open.
        timeout: Duration,
        /// The answer: the identity of the request.
        reply: oneshot::Sender<Result<RequestId, Refusal>>,
    },
}

/// The configuration of the client of the runtime for the station at `connect`.
pub fn client_config(
    connect: std::net::SocketAddr,
    common_address: u16,
    parameters: Parameters,
) -> Result<ClientConfig, LinkError> {
    let mut config = ClientConfig::new(connect, CommonAddress::new(common_address))
        .with_offline_requests(OfflineRequests::Discard)
        .with_parameters(parameters)?;
    config.capacity = CLIENT_CAPACITY;
    Ok(config)
}

/// Runs the client until its requests end or the client stops.
pub async fn run(
    mut client: Client,
    mut requests: mpsc::Receiver<Request>,
    recorder: Arc<Recorder>,
    common_address: CommonAddress,
) {
    loop {
        let due = lock(&recorder.correlation).next_deadline();
        tokio::select! {
            event = client.next_event() => match event {
                Some(event) => record(&recorder, &event),
                None => break,
            },
            request = requests.recv() => match request {
                Some(request) => answer(&client, &recorder, common_address, request).await,
                None => break,
            },
            () = until(due) => {
                let ended = lock(&recorder.correlation).expire(now());
                finish(&recorder, &ended);
            }
        }
    }
}

/// Completes at `due`, or never when no request is open.
async fn until(due: Option<std::time::Instant>) {
    match due {
        Some(at) => tokio::time::sleep_until(TokioInstant::from_std(at)).await,
        None => std::future::pending::<()>().await,
    }
}

/// The clock of the correlator: tokio's, so that a paused clock drives the deadlines.
fn now() -> std::time::Instant {
    TokioInstant::now().into_std()
}

/// Makes the request of the control API, and sends back its answer.
async fn answer(client: &Client, recorder: &Recorder, common: CommonAddress, request: Request) {
    match request {
        Request::StartTransfer(reply) => {
            let result = client
                .start_data_transfer()
                .await
                .map_err(|_| Refusal::Stopped);
            let _ = reply.send(result);
        }
        Request::Interrogate(reply) => {
            let result = match procedures::interrogation(common, Qoi::STATION) {
                Ok(asdu) => {
                    let kind = Kind::Interrogation;
                    submit(client, recorder, kind, asdu, INTERROGATION_TIMEOUT).await
                }
                Err(error) => Err(Refusal::Invalid(error.to_string())),
            };
            let _ = reply.send(result);
        }
        Request::CounterInterrogate { group, reply } => {
            let result = match counter_interrogation(common, group) {
                Ok(asdu) => {
                    let kind = Kind::CounterInterrogation;
                    submit(client, recorder, kind, asdu, INTERROGATION_TIMEOUT).await
                }
                Err(refusal) => Err(refusal),
            };
            let _ = reply.send(result);
        }
        Request::SingleCommand {
            address,
            on,
            select,
            timeout,
            reply,
        } => {
            let result = if transfer_name(recorder) == "started" {
                single_command(client, recorder, common, address, on, select, timeout).await
            } else {
                Err(Refusal::NotStarted)
            };
            let _ = reply.send(result);
        }
    }
}

/// The counter interrogation of a group: 0 asks for every counter (the qualifier's general request,
/// 5), and 1 to 4 for a group. Only the read is asked: freezing and resetting are not requests of the
/// control API.
fn counter_interrogation(common: CommonAddress, group: u8) -> Result<Asdu, Refusal> {
    let request = match group {
        0 => 5,
        1..=4 => group,
        _ => {
            return Err(Refusal::Invalid(
                "a counter group is 0 (every counter) or 1 to 4".to_owned(),
            ))
        }
    };
    let qcc = Qcc::new(request, CounterFreeze::Read)
        .ok_or_else(|| Refusal::Invalid("the counter request".to_owned()))?;
    procedures::counter_interrogation(common, qcc)
        .map_err(|error| Refusal::Invalid(error.to_string()))
}

async fn single_command(
    client: &Client,
    recorder: &Recorder,
    common: CommonAddress,
    address: InformationObjectAddress,
    on: bool,
    select: bool,
    timeout: Duration,
) -> Result<RequestId, Refusal> {
    let qoc = Qoc::new(0, select).ok_or_else(|| Refusal::Invalid("the qualifier".to_owned()))?;
    let command = Sco { on, qoc };
    let asdu = procedures::single_command(common, address, command, None)
        .map_err(|error| Refusal::Invalid(error.to_string()))?;
    submit(
        client,
        recorder,
        Kind::SingleCommand { select },
        asdu,
        timeout,
    )
    .await
}

/// Accepts a request in the correlator, then hands its ASDU to the client. The request is accepted
/// before the ASDU is handed over, so that the events that follow it find it.
async fn submit(
    client: &Client,
    recorder: &Recorder,
    kind: Kind,
    asdu: igs_iec104_codec::asdu::Asdu,
    timeout: Duration,
) -> Result<RequestId, Refusal> {
    let id = lock(&recorder.correlation)
        .submit(kind, asdu.clone(), now(), timeout)
        .map_err(refusal_of)?;
    if client.send_asdu(asdu).await.is_err() {
        // The client has stopped, so the ASDU will not leave: the request is cancelled.
        if let Ok(finished) = lock(&recorder.correlation).cancel(id) {
            finish(recorder, &[finished]);
        }
        return Err(Refusal::Stopped);
    }
    Ok(id)
}

fn refusal_of(error: SubmitError) -> Refusal {
    match error {
        SubmitError::Busy(id) => Refusal::Busy(id),
        SubmitError::TooManyOpen => Refusal::TooManyOpen,
        SubmitError::Malformed => Refusal::Invalid("the request has no address".to_owned()),
    }
}

/// The transfer state, as the status holds it.
pub fn transfer_name(recorder: &Recorder) -> &'static str {
    lock(&recorder.status).transfer
}

/// Records an event of the client, with the connection generation it belongs to, and the verdicts
/// it decides.
fn record(recorder: &Recorder, event: &Event) {
    let kind = kind_of(event);
    let mut ended = Vec::new();
    let generation = match event {
        Event::Connected => {
            let mut correlation = lock(&recorder.correlation);
            correlation.connected();
            correlation.generation()
        }
        Event::Disconnected(_) => {
            let mut correlation = lock(&recorder.correlation);
            ended = correlation.disconnected();
            correlation.generation()
        }
        Event::Discarded(command) => {
            let mut correlation = lock(&recorder.correlation);
            ended = correlation.discarded(command);
            correlation.generation()
        }
        Event::Delivery(Delivery::Transmitted(asdu)) => {
            let mut correlation = lock(&recorder.correlation);
            correlation.transmitted(asdu);
            correlation.generation()
        }
        _ => generation(recorder),
    };
    if let Event::Delivery(Delivery::Asdu(asdu)) = event {
        lock(&recorder.image).observe(asdu, generation, now_ms());
    }
    update(&mut lock(&recorder.status), &kind);
    push(recorder, kind, generation);
    if let Event::Delivery(Delivery::Asdu(asdu)) = event {
        ended = lock(&recorder.correlation).answer(asdu);
    }
    finish(recorder, &ended);
}

/// The current connection generation.
fn generation(recorder: &Recorder) -> u64 {
    lock(&recorder.correlation).generation()
}

/// Records the verdicts of requests that ended.
pub fn finish(recorder: &Recorder, ended: &[Finished]) {
    for finished in ended {
        let kind = verdict_of(finished);
        push(recorder, kind, generation(recorder));
    }
}

fn push(recorder: &Recorder, kind: EventKind, generation: u64) {
    lock(&recorder.log).record(kind, generation);
}

fn verdict_of(finished: &Finished) -> EventKind {
    let (cause, negative) = match finished.outcome {
        Outcome::Refused { cause, negative } => (Some(cause), Some(negative)),
        _ => (None, None),
    };
    EventKind::Verdict {
        request: finished.id,
        outcome: finished.outcome.name(),
        sent: finished.sent,
        cause,
        negative,
        transmitted_generation: finished.generation,
    }
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
            state: transfer_name_of(*state),
        },
        Delivery::Unacknowledged(asdus) => EventKind::Unacknowledged {
            asdus: asdus.iter().map(AsduView::of).collect(),
        },
    }
}

/// The name of a state of the data transfer, as the control API reports it.
pub fn transfer_name_of(state: TransferState) -> &'static str {
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

    #[test]
    fn a_refusal_of_44_to_47_is_reported_with_its_cause() {
        let finished = Finished {
            id: 7,
            outcome: Outcome::Refused {
                cause: 47,
                negative: false,
            },
            sent: true,
            generation: Some(2),
        };
        assert_eq!(
            verdict_of(&finished),
            EventKind::Verdict {
                request: 7,
                outcome: "refused",
                sent: true,
                cause: Some(47),
                negative: Some(false),
                transmitted_generation: Some(2),
            }
        );
    }

    #[test]
    fn the_client_keeps_every_channel_above_the_open_request_bound() {
        let config = client_config(
            "127.0.0.1:2404".parse().expect("address"),
            1,
            Parameters::default(),
        )
        .expect("the defaults are valid");
        assert_eq!(config.capacity, CLIENT_CAPACITY);
    }
}

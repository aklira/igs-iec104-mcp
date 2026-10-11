//! The control API of the client role (task S1). Its routes are versioned under `/v1`; the
//! contract is written in task C1, and until then these routes are not a stable interface.
//!
//! A request is acknowledged when the client has queued it (`202`): it is not yet sent or
//! confirmed. Its outcome comes as events, read with `GET /v1/events`.

use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use tokio::sync::oneshot;

use crate::client_role::{Request, Status};
use crate::events::Recorded;
use crate::lock;
use crate::runtime::Shared;

/// The routes of the control API.
pub fn router(shared: Arc<Shared>) -> Router {
    Router::new()
        .route("/v1/ready", get(ready))
        .route("/v1/transfer/start", post(start_transfer))
        .route("/v1/interrogation/general", post(general_interrogation))
        .route("/v1/events", get(events))
        .with_state(shared)
}

/// The readiness of the runtime, with the connection and the data transfer.
#[derive(Serialize)]
struct Ready {
    ready: bool,
    control_address: String,
    connection: &'static str,
    transfer: &'static str,
}

async fn ready(State(shared): State<Arc<Shared>>) -> Json<Ready> {
    let Status {
        connection,
        transfer,
    } = lock(&shared.recorder.status).clone();
    Json(Ready {
        ready: true,
        control_address: shared.control.to_string(),
        connection,
        transfer,
    })
}

/// The answer to a request: accepted, or the reason it was not queued.
#[derive(Serialize)]
struct Accepted {
    accepted: bool,
    error: Option<String>,
}

async fn start_transfer(State(shared): State<Arc<Shared>>) -> (StatusCode, Json<Accepted>) {
    submit(&shared, Request::StartTransfer).await
}

async fn general_interrogation(State(shared): State<Arc<Shared>>) -> (StatusCode, Json<Accepted>) {
    submit(&shared, Request::GeneralInterrogation).await
}

async fn submit(
    shared: &Shared,
    request: fn(oneshot::Sender<Result<(), String>>) -> Request,
) -> (StatusCode, Json<Accepted>) {
    let (reply, answer) = oneshot::channel();
    let outcome = match shared.requests.send(request(reply)).await {
        Ok(()) => answer
            .await
            .unwrap_or_else(|_| Err(CLIENT_STOPPED.to_owned())),
        Err(_) => Err(CLIENT_STOPPED.to_owned()),
    };
    match outcome {
        Ok(()) => (
            StatusCode::ACCEPTED,
            Json(Accepted {
                accepted: true,
                error: None,
            }),
        ),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(Accepted {
                accepted: false,
                error: Some(error),
            }),
        ),
    }
}

const CLIENT_STOPPED: &str = "the client has stopped";

/// The query of the events: those after `after`. Zero asks for all the events kept.
#[derive(Deserialize)]
struct EventsQuery {
    #[serde(default)]
    after: u64,
}

/// The events after the requested number, and what the reader needs to detect a gap.
#[derive(Serialize)]
struct EventsBody {
    events: Vec<Recorded>,
    /// The oldest sequence number kept, or `null` when no event is kept. A reader that asked
    /// after `n` has missed events when `n + 1` is below this number.
    first_seq: Option<u64>,
    /// The last sequence number recorded.
    last_seq: u64,
    /// The events dropped because the log was full.
    dropped: u64,
}

async fn events(
    State(shared): State<Arc<Shared>>,
    Query(query): Query<EventsQuery>,
) -> Json<EventsBody> {
    let log = lock(&shared.recorder.log);
    Json(EventsBody {
        events: log.after(query.after),
        first_seq: log.first_seq(),
        last_seq: log.last_seq(),
        dropped: log.dropped(),
    })
}

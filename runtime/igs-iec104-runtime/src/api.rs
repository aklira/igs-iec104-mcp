//! The control API (tasks S1 and S2). Its routes are versioned under `/v1`; the contract is
//! written in task C1, and until then these routes are not a stable interface.
//!
//! The client routes answer for a client, and the point routes for a station; the other role
//! answers `409`. A client request is acknowledged when the client has queued it (`202`): it is
//! not yet sent or confirmed. Its outcome comes as events, read with `GET /v1/events`.

use std::sync::Arc;

use crate::correlation::RequestId;
use axum::extract::{Path, Query, State};
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use igs_iec104::clock;
use igs_iec104::process_image::Update;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::oneshot;

use crate::client_role::{self, Refusal, Request as ClientRequest, Status};
use crate::correlation::{CancelError, Outcome, View};
use crate::events::Recorded;
use crate::image::Point;
use crate::lock;
use crate::points::{self, PointSpec};
use crate::runtime::{ClientLink, ServerLink, Shared};
use igs_iec104_codec::formats::Cp56Time2a;
use std::time::Duration;

/// The routes of the control API.
pub fn router(shared: Arc<Shared>) -> Router {
    Router::new()
        .route("/v1/ready", get(ready))
        .route("/v1/transfer/start", post(start_transfer))
        .route("/v1/interrogation/general", post(general_interrogation))
        .route("/v1/commands/single", post(single_command))
        .route("/v1/requests/{id}", get(get_request))
        .route("/v1/requests/{id}/cancel", post(cancel_request))
        .route("/v1/image/points", get(image_points))
        .route("/v1/events", get(events))
        .route("/v1/points", get(list_points))
        .route("/v1/points/{address}", get(get_point).post(update_point))
        // A layer, not a route layer: a request without the token is refused before the router
        // decides whether the path exists.
        .layer(middleware::from_fn_with_state(
            Arc::clone(&shared),
            require_token,
        ))
        .with_state(shared)
}

/// Refuses a request that does not carry the control token (IMPLEMENTATION.md §3).
async fn require_token(
    State(shared): State<Arc<Shared>>,
    request: axum::extract::Request,
    next: Next,
) -> Response {
    if shared.token.accepts(request.headers().get(AUTHORIZATION)) {
        return next.run(request).await;
    }
    let body = json!({"error": "the control token is missing or wrong"});
    (
        StatusCode::UNAUTHORIZED,
        [(WWW_AUTHENTICATE, "Bearer")],
        Json(body),
    )
        .into_response()
}

async fn ready(State(shared): State<Arc<Shared>>) -> Json<Value> {
    let mut body = json!({
        "ready": true,
        "control_address": shared.control.to_string(),
        "contract": crate::CONTRACT_VERSION,
    });
    if let Some(client) = &shared.client {
        let Status {
            connection,
            transfer,
        } = lock(&client.recorder.status).clone();
        body["role"] = json!("client");
        body["connection"] = json!(connection);
        body["transfer"] = json!(transfer);
    }
    if let Some(station) = &shared.server {
        body["role"] = json!("station");
        body["listen_address"] = json!(station.listen.to_string());
        body["points"] = json!(station.points.len());
    }
    Json(body)
}

/// The answer to a client request: accepted with the identity of the request (when it has one), or
/// refused with the reason.
fn accepted(status: StatusCode, request: Option<RequestId>, error: Option<String>) -> Response {
    let body = json!({"accepted": error.is_none(), "request": request, "error": error});
    (status, Json(body)).into_response()
}

/// The answer to a request that was refused, by the status its reason implies.
fn refused(refusal: Refusal) -> Response {
    let status = match refusal {
        Refusal::NotStarted | Refusal::Busy(_) => StatusCode::CONFLICT,
        Refusal::TooManyOpen | Refusal::Stopped => StatusCode::SERVICE_UNAVAILABLE,
        Refusal::Invalid(_) => StatusCode::BAD_REQUEST,
    };
    accepted(status, None, Some(refusal.to_string()))
}

/// The answer to a route of the other role.
fn wrong_role(role: &str) -> Response {
    let error = format!("the runtime does not run as a {role}");
    (StatusCode::CONFLICT, Json(json!({"error": error}))).into_response()
}

/// Asks the client, and waits for its answer to the request.
async fn ask<T>(
    client: &ClientLink,
    request: impl FnOnce(oneshot::Sender<Result<T, Refusal>>) -> ClientRequest,
) -> Result<T, Refusal> {
    let (reply, answer) = oneshot::channel();
    if client.requests.send(request(reply)).await.is_err() {
        return Err(Refusal::Stopped);
    }
    answer.await.unwrap_or(Err(Refusal::Stopped))
}

async fn start_transfer(State(shared): State<Arc<Shared>>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    match ask(client, ClientRequest::StartTransfer).await {
        Ok(()) => accepted(StatusCode::ACCEPTED, None, None),
        Err(refusal) => refused(refusal),
    }
}

async fn general_interrogation(State(shared): State<Arc<Shared>>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    match ask(client, ClientRequest::Interrogate).await {
        Ok(id) => accepted(StatusCode::ACCEPTED, Some(id), None),
        Err(refusal) => refused(refusal),
    }
}

/// The body of a single command: the address, the state, select or execute, and how long the request
/// may stay open.
#[derive(Deserialize)]
struct CommandBody {
    address: u32,
    on: bool,
    #[serde(default)]
    select: bool,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

fn default_timeout_ms() -> u64 {
    10_000
}

/// The longest a request may stay open, in milliseconds.
const MAX_TIMEOUT_MS: u64 = 60_000;

async fn single_command(
    State(shared): State<Arc<Shared>>,
    Json(body): Json<CommandBody>,
) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    let address = match points::address(body.address) {
        Ok(address) => address,
        Err(error) => return refused(Refusal::Invalid(error.to_string())),
    };
    if !(1..=MAX_TIMEOUT_MS).contains(&body.timeout_ms) {
        let reason = format!("timeout_ms must be from 1 to {MAX_TIMEOUT_MS}");
        return refused(Refusal::Invalid(reason));
    }
    let timeout = Duration::from_millis(body.timeout_ms);
    let (on, select) = (body.on, body.select);
    match ask(client, |reply| ClientRequest::SingleCommand {
        address,
        on,
        select,
        timeout,
        reply,
    })
    .await
    {
        Ok(id) => accepted(StatusCode::ACCEPTED, Some(id), None),
        Err(refusal) => refused(refusal),
    }
}

/// A request as the control API reports it.
fn request_view(view: &View) -> Value {
    let (cause, negative) = match view.outcome {
        Some(Outcome::Refused { cause, negative }) => (Some(cause), Some(negative)),
        _ => (None, None),
    };
    json!({
        "id": view.id,
        "kind": view.kind.name(),
        "type_id": view.type_id as u8,
        "address": view.address.value(),
        "stage": view.stage,
        "sent": view.sent,
        "generation": view.generation,
        "outcome": view.outcome.map(Outcome::name),
        "cause": cause,
        "negative": negative,
    })
}

async fn get_request(State(shared): State<Arc<Shared>>, Path(id): Path<u64>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    match lock(&client.recorder.correlation).view(id) {
        Some(view) => Json(request_view(&view)).into_response(),
        None => no_request(id),
    }
}

async fn cancel_request(State(shared): State<Arc<Shared>>, Path(id): Path<u64>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    let outcome = lock(&client.recorder.correlation).cancel(id);
    match outcome {
        Ok(finished) => client_role::finish(&client.recorder, &[finished]),
        Err(CancelError::Unknown) => return no_request(id),
        Err(CancelError::Ended(ended)) => {
            let error = format!("the request already ended: {}", ended.name());
            return (StatusCode::CONFLICT, Json(json!({"error": error}))).into_response();
        }
    }
    match lock(&client.recorder.correlation).view(id) {
        Some(view) => Json(request_view(&view)).into_response(),
        None => no_request(id),
    }
}

fn no_request(id: RequestId) -> Response {
    let body = json!({"error": format!("no request with identity {id}")});
    (StatusCode::NOT_FOUND, Json(body)).into_response()
}

/// A time tag as text: `20yy-mm-ddThh:mm:ss.mmm`, with the two-digit year of CP56Time2a read as
/// 2000 plus that year.
fn time_text(time: Cp56Time2a) -> String {
    let milliseconds = u32::from(time.milliseconds());
    format!(
        "20{:02}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}",
        time.year(),
        time.month(),
        time.day_of_month(),
        time.hours(),
        time.minutes(),
        milliseconds / 1000,
        milliseconds % 1000,
    )
}

/// A point of the client's image as the control API reports it.
fn image_view(address: u32, point: &Point) -> Value {
    json!({
        "address": address,
        "type_id": point.type_id as u8,
        "value": points::json_of(point.value),
        "quality": {
            "invalid": point.quality.invalid,
            "not_topical": point.quality.not_topical,
            "substituted": point.quality.substituted,
            "blocked": point.quality.blocked,
            "overflow": point.quality.overflow,
        },
        "time": point.time.map(time_text),
        "cause": point.cause,
        "observed_at_ms": point.observed_at_ms,
        "generation": point.generation,
    })
}

async fn image_points(State(shared): State<Arc<Shared>>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    let image = lock(&client.recorder.image);
    let points: Vec<Value> = image
        .iter()
        .map(|(address, point)| image_view(address, point))
        .collect();
    Json(json!({"points": points})).into_response()
}

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

async fn events(State(shared): State<Arc<Shared>>, Query(query): Query<EventsQuery>) -> Response {
    let Some(client) = &shared.client else {
        return wrong_role("client");
    };
    let log = lock(&client.recorder.log);
    Json(EventsBody {
        events: log.after(query.after),
        first_seq: log.first_seq(),
        last_seq: log.last_seq(),
        dropped: log.dropped(),
    })
    .into_response()
}

/// A point as the control API reports it: its configuration, and its current value.
fn point_view(station: &ServerLink, point: &PointSpec) -> Value {
    let value = points::address(point.address)
        .ok()
        .and_then(|address| station.handle.value(address))
        .map_or(Value::Null, points::json_of);
    json!({
        "address": point.address,
        "kind": point.kind,
        "stamped": point.stamped,
        "value": value,
    })
}

async fn list_points(State(shared): State<Arc<Shared>>) -> Response {
    let Some(station) = &shared.server else {
        return wrong_role("station");
    };
    let views: Vec<Value> = station
        .points
        .values()
        .map(|point| point_view(station, point))
        .collect();
    Json(json!({"points": views})).into_response()
}

async fn get_point(State(shared): State<Arc<Shared>>, Path(address): Path<u32>) -> Response {
    let Some(station) = &shared.server else {
        return wrong_role("station");
    };
    match station.points.get(&address) {
        Some(point) => Json(point_view(station, point)).into_response(),
        None => no_point(address),
    }
}

/// The body of a value update: the new value, in the form of the point's kind.
#[derive(Deserialize)]
struct UpdateBody {
    value: Value,
}

async fn update_point(
    State(shared): State<Arc<Shared>>,
    Path(address): Path<u32>,
    Json(body): Json<UpdateBody>,
) -> Response {
    let Some(station) = &shared.server else {
        return wrong_role("station");
    };
    let Some(point) = station.points.get(&address) else {
        return no_point(address);
    };
    let value = match points::value_of(point.kind, address, &body.value) {
        Ok(value) => value,
        Err(error) => {
            let body = json!({"error": error.to_string()});
            return (StatusCode::BAD_REQUEST, Json(body)).into_response();
        }
    };
    let Ok(information_address) = points::address(address) else {
        return no_point(address);
    };
    let time = match clock::now_utc() {
        Ok(time) => time,
        Err(error) => {
            let body = json!({"error": format!("the clock is not available: {error}")});
            return (StatusCode::INTERNAL_SERVER_ERROR, Json(body)).into_response();
        }
    };
    match station.handle.update(information_address, value, time) {
        Ok(outcome) => Json(json!({
            "address": address,
            "value": points::json_of(value),
            "queued": matches!(outcome, Update::Queued),
        }))
        .into_response(),
        Err(error) => {
            let body = json!({"error": format!("the value cannot be published: {error}")});
            (StatusCode::INTERNAL_SERVER_ERROR, Json(body)).into_response()
        }
    }
}

fn no_point(address: u32) -> Response {
    let body = json!({"error": format!("no point at address {address}")});
    (StatusCode::NOT_FOUND, Json(body)).into_response()
}

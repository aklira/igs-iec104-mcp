//! The control API (tasks S1 and S2). Its routes are versioned under `/v1`; the contract is
//! written in task C1, and until then these routes are not a stable interface.
//!
//! The client routes answer for a client, and the point routes for a station; the other role
//! answers `409`. A client request is acknowledged when the client has queued it (`202`): it is
//! not yet sent or confirmed. Its outcome comes as events, read with `GET /v1/events`.

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use igs_iec104::clock;
use igs_iec104::process_image::Update;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::oneshot;

use crate::client_role::{Request, Status};
use crate::events::Recorded;
use crate::lock;
use crate::points::{self, PointSpec};
use crate::runtime::{ClientLink, ServerLink, Shared};

/// The routes of the control API.
pub fn router(shared: Arc<Shared>) -> Router {
    Router::new()
        .route("/v1/ready", get(ready))
        .route("/v1/transfer/start", post(start_transfer))
        .route("/v1/interrogation/general", post(general_interrogation))
        .route("/v1/events", get(events))
        .route("/v1/points", get(list_points))
        .route("/v1/points/{address}", get(get_point).post(update_point))
        .with_state(shared)
}

async fn ready(State(shared): State<Arc<Shared>>) -> Json<Value> {
    let mut body = json!({
        "ready": true,
        "control_address": shared.control.to_string(),
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

/// The answer to a client request that cannot be queued, or that was queued.
fn accepted(status: StatusCode, accepted: bool, error: Option<String>) -> Response {
    (status, Json(json!({"accepted": accepted, "error": error}))).into_response()
}

/// The answer to a route of the other role.
fn wrong_role(role: &str) -> Response {
    let error = format!("the runtime does not run as a {role}");
    (StatusCode::CONFLICT, Json(json!({"error": error}))).into_response()
}

async fn start_transfer(State(shared): State<Arc<Shared>>) -> Response {
    match &shared.client {
        Some(client) => submit(client, Request::StartTransfer).await,
        None => wrong_role("client"),
    }
}

async fn general_interrogation(State(shared): State<Arc<Shared>>) -> Response {
    match &shared.client {
        Some(client) => submit(client, Request::GeneralInterrogation).await,
        None => wrong_role("client"),
    }
}

async fn submit(
    client: &ClientLink,
    request: fn(oneshot::Sender<Result<(), String>>) -> Request,
) -> Response {
    let (reply, answer) = oneshot::channel();
    let outcome = match client.requests.send(request(reply)).await {
        Ok(()) => answer
            .await
            .unwrap_or_else(|_| Err(CLIENT_STOPPED.to_owned())),
        Err(_) => Err(CLIENT_STOPPED.to_owned()),
    };
    match outcome {
        Ok(()) => accepted(StatusCode::ACCEPTED, true, None),
        Err(error) => accepted(StatusCode::SERVICE_UNAVAILABLE, false, Some(error)),
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

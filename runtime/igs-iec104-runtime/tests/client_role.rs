//! Task S1: the control API of the client role, against a station of the upstream library.
//!
//! The station runs in this process, with the upstream server (the stack of `igs104-server`).
//! The manual check against the demo binary is recorded in `docs/ai-log/S1.md`.

use std::net::SocketAddr;
use std::time::Duration;

use igs_iec104::process_image::PointValue;
use igs_iec104::server::{Handler, Operation, Refusal, Server, ServerConfig};
use igs_iec104_codec::elements::{QualityFlags, Siq};
use igs_iec104_codec::header::{CommonAddress, InformationObjectAddress};
use igs_iec104_runtime::config::{Config, DEFAULT_CONTROL, DEFAULT_EVENT_CAPACITY};
use igs_iec104_runtime::runtime::Runtime;
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::oneshot;
use tokio::time::{sleep, timeout};

/// The longest a test waits for the runtime to reach a state.
const PATIENCE: Duration = Duration::from_secs(10);

/// A station that refuses every command: the tests only read from it.
struct Refuse;

impl Handler for Refuse {
    fn select(&self, _: &Operation) -> Result<(), Refusal> {
        Err(Refusal::UnknownAddress)
    }

    fn execute(&self, _: &Operation) -> Result<(), Refusal> {
        Err(Refusal::UnknownAddress)
    }
}

/// Starts a station with one single-point status, on a port it chooses, and returns its address.
async fn station() -> SocketAddr {
    let bind: SocketAddr = "127.0.0.1:0".parse().expect("address");
    let server = Server::bind(ServerConfig::new(bind, CommonAddress::new(1)), Refuse)
        .await
        .expect("the station binds");
    let address = server.local_addr().expect("the station has an address");
    let status = InformationObjectAddress::new(1).expect("an address in range");
    let on = PointValue::Single(Siq {
        on: true,
        quality: QualityFlags::default(),
    });
    server
        .handle()
        .add_point(status, on, false)
        .expect("the point is added");
    tokio::spawn(server.run());
    address
}

fn config(connect: SocketAddr) -> Config {
    Config {
        connect,
        common_address: 1,
        control: DEFAULT_CONTROL,
        event_capacity: DEFAULT_EVENT_CAPACITY,
    }
}

/// One HTTP request to the control API. It returns the status code and the JSON body.
async fn call(control: SocketAddr, method: &str, path: &str) -> (u16, Value) {
    let mut stream = TcpStream::connect(control)
        .await
        .expect("the control API accepts");
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {control}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .await
        .expect("the request is written");
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .await
        .expect("the response is read");
    let text = String::from_utf8(response).expect("the response is text");
    let (head, body) = text
        .split_once("\r\n\r\n")
        .expect("the response has a body");
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .expect("the status line has a code");
    (
        status,
        serde_json::from_str(body).expect("the body is JSON"),
    )
}

/// Polls `path` until `reached` accepts its body, and returns that body.
async fn wait_for(control: SocketAddr, path: &str, reached: impl Fn(&Value) -> bool) -> Value {
    timeout(PATIENCE, async {
        loop {
            let (_, body) = call(control, "GET", path).await;
            if reached(&body) {
                return body;
            }
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("the runtime reached the state within the patience of the test")
}

/// Whether an event of the body matches `matches`.
fn has_event(body: &Value, matches: impl Fn(&Value) -> bool) -> bool {
    body["events"]
        .as_array()
        .is_some_and(|events| events.iter().any(matches))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_control_api_starts_the_transfer_and_returns_the_answers_of_an_interrogation() {
    let runtime = Runtime::bind(config(station().await))
        .await
        .expect("the runtime binds");
    let control = runtime.control_address();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tokio::spawn(runtime.serve(async {
        let _ = stopped.await;
    }));

    let ready = wait_for(control, "/v1/ready", |body| body["connection"] == "up").await;
    assert_eq!(ready["ready"], true);
    assert_eq!(ready["control_address"], control.to_string());
    assert_eq!(ready["transfer"], "stopped");

    let (status, body) = call(control, "POST", "/v1/transfer/start").await;
    assert_eq!(status, 202);
    assert_eq!(body["accepted"], true);
    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| {
            event["kind"] == "transfer" && event["state"] == "started"
        })
    })
    .await;

    let (status, body) = call(control, "POST", "/v1/interrogation/general").await;
    assert_eq!(status, 202);
    assert_eq!(body["accepted"], true);
    let events = wait_for(control, "/v1/events", |body| {
        has_event(body, |event| {
            event["kind"] == "asdu" && event["asdu"]["cause"] == 10
        })
    })
    .await;

    // The station answers with its point, a single point (M_SP_NA_1) interrogated by the station
    // (cause 20), then ends the interrogation (cause 10).
    let asdus: Vec<&Value> = events["events"]
        .as_array()
        .expect("the events are a list")
        .iter()
        .filter(|event| event["kind"] == "asdu")
        .map(|event| &event["asdu"])
        .collect();
    assert!(asdus.iter().any(|asdu| {
        asdu["type_id"] == 1 && asdu["cause"] == 20 && asdu["common_address"] == 1
    }));
    assert!(asdus.iter().any(|asdu| asdu["cause"] == 10));
    assert!(asdus.iter().all(|asdu| asdu["bytes_hex"].is_string()));

    assert_eq!(events["first_seq"], 1);
    assert_eq!(events["dropped"], 0);

    let _ = stop.send(());
    serving
        .await
        .expect("the runtime task ends")
        .expect("the control API stops without an error");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_request_made_without_a_connection_is_reported_as_discarded() {
    // A port that nothing listens on: the client cannot connect.
    let closed = {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("a port is free");
        listener.local_addr().expect("the port has an address")
    };
    let runtime = Runtime::bind(config(closed))
        .await
        .expect("the runtime binds");
    let control = runtime.control_address();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tokio::spawn(runtime.serve(async {
        let _ = stopped.await;
    }));

    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "connect_failed")
    })
    .await;
    let (status, body) = call(control, "POST", "/v1/transfer/start").await;
    assert_eq!(status, 202, "the request is queued, not refused: {body}");

    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "discarded")
    })
    .await;
    let (_, ready) = call(control, "GET", "/v1/ready").await;
    assert_eq!(ready["connection"], "down");
    assert_eq!(ready["transfer"], "stopped");

    let _ = stop.send(());
    serving
        .await
        .expect("the runtime task ends")
        .expect("the control API stops without an error");
}

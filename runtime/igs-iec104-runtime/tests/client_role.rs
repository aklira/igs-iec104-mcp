//! Task S1: the control API of the client role, against a station of the upstream library.
//!
//! The station runs in this process, with the upstream server (the stack of `igs104-server`).
//! The manual check against the demo binary is recorded in `docs/ai-log/S1.md`.

mod common;

use igs_iec104_link::Parameters;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

use common::{get, has_event, post, send, wait_for, TokenFile};
use igs_iec104::process_image::PointValue;
use igs_iec104::server::{Handler, Operation, Refusal, Server, ServerConfig};
use igs_iec104_codec::elements::{Bcr, QualityFlags, Siq};
use igs_iec104_codec::header::{CommonAddress, InformationObjectAddress};
use igs_iec104_runtime::config::{Config, Role, DEFAULT_CONTROL, DEFAULT_EVENT_CAPACITY};
use igs_iec104_runtime::runtime::Runtime;
use tokio::sync::oneshot;

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
    let counter = InformationObjectAddress::new(2).expect("an address in range");
    let reading = PointValue::Counter(Bcr::new(42, 0).expect("a counter"));
    server
        .handle()
        .add_point(counter, reading, false)
        .expect("the counter is added");
    tokio::spawn(server.run());
    address
}

fn config(connect: SocketAddr, token: &TokenFile) -> Config {
    Config {
        role: Role::Client { connect },
        common_address: 1,
        control: DEFAULT_CONTROL,
        event_capacity: DEFAULT_EVENT_CAPACITY,
        parameters: Parameters::default(),
        control_token: token.0.clone(),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_control_api_starts_the_transfer_and_returns_the_answers_of_an_interrogation() {
    let token = TokenFile::new("s1-interrogation");
    let runtime = Runtime::bind(config(station().await, &token))
        .await
        .expect("the runtime binds");
    let control = runtime.control_address();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tokio::spawn(runtime.serve(async {
        let _ = stopped.await;
    }));

    let ready = wait_for(control, "/v1/ready", |body| body["connection"] == "up").await;
    assert_eq!(ready["ready"], true);
    assert_eq!(ready["role"], "client");
    assert_eq!(ready["control_address"], control.to_string());
    assert_eq!(ready["transfer"], "stopped");

    let (status, body) = send(control, "POST", "/v1/transfer/start", None).await;
    assert_eq!(status, 202);
    assert_eq!(body["accepted"], true);
    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| {
            event["kind"] == "transfer" && event["state"] == "started"
        })
    })
    .await;

    let (status, body) = send(control, "POST", "/v1/interrogation/general", None).await;
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
    let asdus: Vec<&serde_json::Value> = events["events"]
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
    let token = TokenFile::new("s1-discarded");
    // A port that nothing listens on: the client cannot connect.
    let closed = {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("a port is free");
        listener.local_addr().expect("the port has an address")
    };
    let runtime = Runtime::bind(config(closed, &token))
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
    let (status, body) = send(control, "POST", "/v1/transfer/start", None).await;
    assert_eq!(status, 202, "the request is queued, not refused: {body}");

    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "discarded")
    })
    .await;
    let (_, ready) = get(control, "/v1/ready").await;
    assert_eq!(ready["connection"], "down");
    assert_eq!(ready["transfer"], "stopped");

    let (status, _) = post(control, "/v1/points/1", &serde_json::json!({"value": true})).await;
    assert_eq!(status, 409, "a client does not serve points");

    let _ = stop.send(());
    serving
        .await
        .expect("the runtime task ends")
        .expect("the control API stops without an error");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_counter_interrogation_brings_the_counters_into_the_image() {
    let token = TokenFile::new("s4b-counter");
    let runtime = Runtime::bind(config(station().await, &token))
        .await
        .expect("the runtime binds");
    let control = runtime.control_address();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tokio::spawn(runtime.serve(async {
        let _ = stopped.await;
    }));
    wait_for(control, "/v1/ready", |body| body["connection"] == "up").await;
    send(control, "POST", "/v1/transfer/start", None).await;
    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| {
            event["kind"] == "transfer" && event["state"] == "started"
        })
    })
    .await;

    // A group that does not exist is refused by the control API, before anything is sent.
    let (status, body) = send(
        control,
        "POST",
        "/v1/interrogation/counter",
        Some(&serde_json::json!({"group": 5})),
    )
    .await;
    assert_eq!(status, 400, "{body}");

    let (status, body) = send(
        control,
        "POST",
        "/v1/interrogation/counter",
        Some(&serde_json::json!({})),
    )
    .await;
    assert_eq!(status, 202);
    assert_eq!(body["accepted"], true);
    let events = wait_for(control, "/v1/events", |body| {
        has_event(body, |event| {
            event["kind"] == "asdu"
                && event["asdu"]["type_id"] == 101
                && event["asdu"]["cause"] == 10
        })
    })
    .await;

    // The station answers with its counter (M_IT_NA_1, cause 37) and ends the request (cause 10).
    let asdus: Vec<&serde_json::Value> = events["events"]
        .as_array()
        .expect("the events are a list")
        .iter()
        .filter(|event| event["kind"] == "asdu")
        .map(|event| &event["asdu"])
        .collect();
    assert!(asdus
        .iter()
        .any(|asdu| asdu["type_id"] == 15 && asdu["cause"] == 37));

    let image = wait_for(control, "/v1/image/points", |body| {
        body["points"]
            .as_array()
            .is_some_and(|points| points.iter().any(|point| point["address"] == 2))
    })
    .await;
    let counter = image["points"]
        .as_array()
        .expect("a list")
        .iter()
        .find(|point| point["address"] == 2)
        .expect("the counter is in the image");
    assert_eq!(counter["value"], 42);
    assert_eq!(counter["type_id"], 15);

    let _ = stop.send(());
    serving
        .await
        .expect("the runtime task ends")
        .expect("the control API stops without an error");
}

/// A peer that accepts connections and never answers: the frames the client sends are not confirmed.
async fn silent_peer() -> SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("a port is free");
    let address = listener.local_addr().expect("the port has an address");
    tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((stream, _)) = listener.accept().await {
            held.push(stream);
        }
    });
    address
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_short_t1_ends_a_link_whose_start_is_never_confirmed_sooner_than_the_default() {
    let token = TokenFile::new("s5-t1");
    let mut config = config(silent_peer().await, &token);
    config.parameters = Parameters {
        t1: Duration::from_secs(2),
        t2: Duration::from_secs(1),
        ..Parameters::default()
    };
    let runtime = Runtime::bind(config).await.expect("the runtime binds");
    let control = runtime.control_address();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tokio::spawn(runtime.serve(async {
        let _ = stopped.await;
    }));
    wait_for(control, "/v1/ready", |body| body["connection"] == "up").await;

    let started = Instant::now();
    let (status, _) = send(control, "POST", "/v1/transfer/start", None).await;
    assert_eq!(status, 202);
    // The start is never confirmed, so t1 ends the link: the client reports the end of the
    // connection. The default t1 of 15 seconds would not end it within the patience of the test.
    wait_for(control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "disconnected")
    })
    .await;
    let elapsed = started.elapsed();
    assert!(
        elapsed < Duration::from_secs(9),
        "the link ended after {elapsed:?}, not after t1"
    );

    let _ = stop.send(());
    serving
        .await
        .expect("the runtime task ends")
        .expect("the control API stops without an error");
}

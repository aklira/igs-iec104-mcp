//! Task C3: the station of the runtime, with its command policy, its connection events, its stop, and
//! its losses. The controller is the upstream client, so that the station is seen as a peer sees it.

mod common;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use common::{get, has_event, post, wait_for, TokenFile};
use igs_iec104::{Client, ClientConfig, Delivery, Event};
use igs_iec104_codec::asdu::{Asdu, Body, Objects};
use igs_iec104_codec::elements::{Qoc, Sco};
use igs_iec104_codec::header::{cause, CommonAddress, InformationObjectAddress};
use igs_iec104_link::TransferState;
use igs_iec104_runtime::config::{Config, Role, DEFAULT_CONTROL};
use igs_iec104_runtime::runtime::{Runtime, Stopped};
use serde_json::Value;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio::time::timeout;

const PATIENCE: Duration = Duration::from_secs(10);

/// A points file of one test, removed when the test ends.
struct PointsFile(PathBuf);

impl PointsFile {
    fn new(name: &str, text: &str) -> Self {
        let path = std::env::temp_dir().join(format!("igs-c3-{}-{name}.json", std::process::id()));
        std::fs::write(&path, text).expect("the points file is written");
        Self(path)
    }
}

impl Drop for PointsFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// Three points: a single point that accepts commands, a double point that refuses them, and a
/// single point with no command, which the station refuses as an unknown address.
const POINTS: &str = r#"{"schema_version": 1, "points": [
    {"address": 1, "kind": "single", "value": false, "command": "accept"},
    {"address": 2, "kind": "double", "value": "off", "command": "refuse"},
    {"address": 3, "kind": "single", "value": true}
]}"#;

/// A station runtime, running, with its addresses and the way to stop it.
struct Station {
    control: SocketAddr,
    listen: SocketAddr,
    stop: oneshot::Sender<()>,
    serving: JoinHandle<std::io::Result<Stopped>>,
}

impl Station {
    async fn start(
        points: &PointsFile,
        event_capacity: usize,
        selection_timeout: Duration,
        token: &TokenFile,
    ) -> Self {
        let config = Config {
            role: Role::Server {
                listen: "127.0.0.1:0".parse().expect("address"),
                points: points.0.clone(),
                selection_timeout,
            },
            common_address: 1,
            control: DEFAULT_CONTROL,
            event_capacity,
            control_token: token.0.clone(),
        };
        let runtime = Runtime::bind(config).await.expect("the runtime binds");
        let control = runtime.control_address();
        let listen = runtime.station_address().expect("the runtime is a station");
        let (stop, stopped) = oneshot::channel::<()>();
        let serving = tokio::spawn(runtime.serve(async {
            let _ = stopped.await;
        }));
        wait_for(control, "/v1/ready", |body| body["role"] == "station").await;
        Self {
            control,
            listen,
            stop,
            serving,
        }
    }

    /// Stops the station, and returns what the stop reported.
    async fn stop(self) -> Stopped {
        let _ = self.stop.send(());
        self.serving
            .await
            .expect("the runtime task ends")
            .expect("the control API stops without an error")
    }
}

/// A controller connected to the station and started.
async fn controller(station: SocketAddr) -> Client {
    let mut client = Client::connect(ClientConfig::new(station, CommonAddress::new(1)));
    client
        .start_data_transfer()
        .await
        .expect("the transfer request is queued");
    timeout(PATIENCE, async {
        while let Some(event) = client.next_event().await {
            if matches!(
                event,
                Event::Delivery(Delivery::Transfer(TransferState::Started))
            ) {
                return;
            }
        }
    })
    .await
    .expect("the transfer starts");
    client
}

/// The single command of `address`, select or execute, as the controller sends it.
fn single_command(address: u32, select: bool) -> Asdu {
    let command = Sco {
        on: true,
        qoc: Qoc::new(0, select).expect("a qualifier"),
    };
    igs_iec104::procedures::single_command(
        CommonAddress::new(1),
        InformationObjectAddress::new(address).expect("an address"),
        command,
        None,
    )
    .expect("the command is built")
}

/// The station's answer to a command of `address`: its cause and P/N bit.
async fn answer_of(client: &mut Client, address: u32) -> (u8, bool) {
    timeout(PATIENCE, async {
        while let Some(event) = client.next_event().await {
            if let Event::Delivery(Delivery::Asdu(asdu)) = event {
                if is_command_answer(&asdu, address) {
                    return (asdu.cot.cause(), asdu.cot.negative);
                }
            }
        }
        panic!("the connection ended before the answer");
    })
    .await
    .expect("the station answers within the patience of the test")
}

fn is_command_answer(asdu: &Asdu, address: u32) -> bool {
    matches!(asdu.body, Body::C_SC_NA_1(_))
        && asdu.body.first_address() == InformationObjectAddress::new(address)
}

async fn send_command(client: &Client, address: u32, select: bool) {
    client
        .send_asdu(single_command(address, select))
        .await
        .expect("the command is queued");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn commands_are_judged_by_the_policy_of_their_point_and_logged() {
    let points = PointsFile::new("policy", POINTS);
    let token = TokenFile::new("c3-policy");
    let station = Station::start(&points, 1024, Duration::from_secs(30), &token).await;
    let mut client = controller(station.listen).await;

    // An accepted selection: confirmed by its activation confirmation, positively.
    send_command(&client, 1, true).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, false)
    );

    // A refused command: a negative confirmation.
    send_command(&client, 2, true).await;
    assert_eq!(
        answer_of(&mut client, 2).await,
        (cause::ACTIVATION_CONFIRMATION, true)
    );

    // A point with no command: an unknown address, as the station answers it.
    send_command(&client, 3, true).await;
    assert_eq!(
        answer_of(&mut client, 3).await,
        (cause::UNKNOWN_INFO_OBJECT_ADDRESS, true)
    );

    let (status, body) = get(station.control, "/v1/station/events").await;
    assert_eq!(status, 200);
    let judged: Vec<&Value> = body["events"]
        .as_array()
        .expect("a list")
        .iter()
        .filter(|event| event["kind"] == "command")
        .collect();
    let accepted: Vec<(u64, bool)> = judged
        .iter()
        .map(|event| {
            (
                event["address"].as_u64().unwrap_or_default(),
                event["accepted"] == true,
            )
        })
        .collect();
    assert_eq!(accepted, vec![(1, true), (2, false), (3, false)]);
    assert!(judged
        .iter()
        .all(|event| event["command"] == "single" && event["select"] == true));

    drop(client);
    station.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_selection_expires_and_an_execution_then_needs_a_new_selection() {
    let points = PointsFile::new("expiry", POINTS);
    let token = TokenFile::new("c3-expiry");
    // A short selection time-out, so that the test does not wait for the default of 30 seconds.
    let station = Station::start(&points, 1024, Duration::from_millis(300), &token).await;
    let mut client = controller(station.listen).await;

    // An execution without a selection is refused, as select-before-operate requires.
    send_command(&client, 1, false).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, true)
    );

    // A selection then an execution, at once: the execution is confirmed by its termination.
    send_command(&client, 1, true).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, false)
    );
    send_command(&client, 1, false).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, false)
    );
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_TERMINATION, false)
    );

    // A selection that waits longer than the time-out: its execution is refused again.
    send_command(&client, 1, true).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, false)
    );
    tokio::time::sleep(Duration::from_millis(600)).await;
    send_command(&client, 1, false).await;
    assert_eq!(
        answer_of(&mut client, 1).await,
        (cause::ACTIVATION_CONFIRMATION, true)
    );

    drop(client);
    station.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_client_that_connects_starts_and_leaves_is_reported_in_order() {
    let points = PointsFile::new("connection", POINTS);
    let token = TokenFile::new("c3-connection");
    let station = Station::start(&points, 1024, Duration::from_secs(30), &token).await;
    let client = controller(station.listen).await;
    drop(client);

    let closed = wait_for(station.control, "/v1/station/events", |body| {
        has_event(body, |event| event["kind"] == "closed")
    })
    .await;
    let events = closed["events"].as_array().expect("a list");
    let position = |kind: &str| events.iter().position(|event| event["kind"] == kind);
    let (connected, transfer, end) = (
        position("connected").expect("a connection was reported"),
        position("transfer").expect("the transfer was reported"),
        position("closed").expect("the end was reported"),
    );
    assert!(connected < transfer && transfer < end, "{events:?}");
    assert_eq!(events[transfer]["state"], "started");
    assert_eq!(
        events[end]["end"], "transport",
        "the peer closed the stream"
    );
    assert_eq!(events[connected]["connection"], events[end]["connection"]);

    station.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_stop_reports_the_connections_it_had() {
    let points = PointsFile::new("stop", POINTS);
    let token = TokenFile::new("c3-stop");
    let station = Station::start(&points, 1024, Duration::from_secs(30), &token).await;
    let mut client = controller(station.listen).await;
    // The controller keeps reading its events while the station stops, so that the station can drain.
    let reading = tokio::spawn(async move { while client.next_event().await.is_some() {} });

    let stopped = station.stop().await;
    reading.abort();
    let report = stopped.station.expect("a station reports its stop");
    assert_eq!(
        report.connections, 1,
        "the connection open at the stop is reported"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn events_lost_from_a_full_stream_are_reported() {
    let points = PointsFile::new("losses", POINTS);
    let token = TokenFile::new("c3-losses");
    // A stream of one event: a burst of connections must lose some of its events.
    let station = Station::start(&points, 1, Duration::from_secs(30), &token).await;
    let mut peers = Vec::new();
    for _ in 0..40 {
        peers.push(
            tokio::net::TcpStream::connect(station.listen)
                .await
                .expect("a connection"),
        );
    }

    let body = wait_for(station.control, "/v1/station/events", |body| {
        body["dropped"].as_u64().is_some_and(|dropped| dropped > 0)
    })
    .await;
    assert!(body["dropped"].as_u64().unwrap_or_default() > 0, "{body}");
    drop(peers);
    station.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_counter_point_is_served_and_its_update_reaches_the_client() {
    let points = PointsFile::new(
        "counter",
        r#"{"schema_version": 1, "points": [{"address": 4, "kind": "counter", "value": 7}]}"#,
    );
    let token = TokenFile::new("c4-counter");
    let station = Station::start(&points, 1024, Duration::from_secs(30), &token).await;
    let mut client = controller(station.listen).await;

    let (status, body) = get(station.control, "/v1/points/4").await;
    assert_eq!(status, 200);
    assert_eq!(body["kind"], "counter");
    assert_eq!(body["value"], 7);

    let (status, _) = post(
        station.control,
        "/v1/points/4",
        &serde_json::json!({"value": 9}),
    )
    .await;
    assert_eq!(status, 200);

    // The update leaves as a spontaneous counter reading (M_IT_NA_1) with the new value.
    let reading = timeout(PATIENCE, async {
        while let Some(event) = client.next_event().await {
            if let Event::Delivery(Delivery::Asdu(asdu)) = event {
                if matches!(asdu.body, Body::M_IT_NA_1(_))
                    && asdu.body.first_address() == InformationObjectAddress::new(4)
                {
                    return asdu;
                }
            }
        }
        panic!("the connection ended before the reading");
    })
    .await
    .expect("the reading arrives within the patience of the test");
    let Body::M_IT_NA_1(Objects::Individual(objects)) = &reading.body else {
        panic!("a counter reading is an individual object list");
    };
    assert_eq!(objects[0].value.counter(), 9);
    assert_eq!(reading.cot.cause(), cause::SPONTANEOUS);

    drop(client);
    station.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_step_and_a_bitstring_point_are_served_and_their_updates_reach_the_client() {
    let points = PointsFile::new(
        "step-bits",
        r#"{"schema_version": 1, "points": [
            {"address": 5, "kind": "step", "value": {"value": 3, "transient": false}},
            {"address": 6, "kind": "bitstring", "value": 0}
        ]}"#,
    );
    let token = TokenFile::new("s4c-step-bits");
    let station = Station::start(&points, 1024, Duration::from_secs(30), &token).await;
    let mut client = controller(station.listen).await;

    let (status, body) = get(station.control, "/v1/points/5").await;
    assert_eq!(status, 200);
    assert_eq!(
        body["value"],
        serde_json::json!({"value": 3, "transient": false})
    );

    let (status, _) = post(
        station.control,
        "/v1/points/5",
        &serde_json::json!({"value": {"value": -2, "transient": true}}),
    )
    .await;
    assert_eq!(status, 200);
    let (status, _) = post(
        station.control,
        "/v1/points/6",
        &serde_json::json!({"value": 9}),
    )
    .await;
    assert_eq!(status, 200);

    // The updates leave as spontaneous readings: a step (M_ST_NA_1) and a bitstring (M_BO_NA_1).
    let step = next_reading(&mut client, Body::M_ST_NA_1(Objects::Individual(vec![])), 5).await;
    let Body::M_ST_NA_1(Objects::Individual(objects)) = &step.body else {
        panic!("a step reading is an individual object list");
    };
    assert_eq!(objects[0].value.0.value(), -2);
    assert!(objects[0].value.0.is_transient());

    let bits = next_reading(&mut client, Body::M_BO_NA_1(Objects::Individual(vec![])), 6).await;
    let Body::M_BO_NA_1(Objects::Individual(objects)) = &bits.body else {
        panic!("a bitstring reading is an individual object list");
    };
    // 9 is 2^0 + 2^3: bits 1 and 4 are on, as the number of each bit (1 to 32) gives.
    let state = objects[0].value.0;
    assert_eq!(state.bit(1), Some(true));
    assert_eq!(state.bit(2), Some(false));
    assert_eq!(state.bit(4), Some(true));

    drop(client);
    station.stop().await;
}

/// The next spontaneous reading of the type of `shape`, for the address `address`.
async fn next_reading(client: &mut Client, shape: Body, address: u32) -> Asdu {
    let type_id = shape.type_id();
    timeout(PATIENCE, async {
        while let Some(event) = client.next_event().await {
            if let Event::Delivery(Delivery::Asdu(asdu)) = event {
                if asdu.body.type_id() == type_id
                    && asdu.body.first_address() == InformationObjectAddress::new(address)
                {
                    return asdu;
                }
            }
        }
        panic!("the connection ended before the reading");
    })
    .await
    .expect("the reading arrives within the patience of the test")
}

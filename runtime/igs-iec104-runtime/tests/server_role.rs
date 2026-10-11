//! Task S2: the server role. An upstream client interrogates a station of the runtime, and reads
//! a point that was updated through the control API.

mod common;

use igs_iec104_link::Parameters;
use std::net::SocketAddr;
use std::path::PathBuf;

use common::{get, post, send, send_with, wait_for, TokenFile, PATIENCE};
use igs_iec104::{Client, ClientConfig, Delivery, Event};
use igs_iec104_codec::asdu::{Asdu, Body, Objects};
use igs_iec104_codec::elements::Qoi;
use igs_iec104_codec::header::CommonAddress;
use igs_iec104_link::TransferState;
use igs_iec104_runtime::config::{Config, Role, DEFAULT_CONTROL, DEFAULT_EVENT_CAPACITY};
use igs_iec104_runtime::runtime::Runtime;
use serde_json::json;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio::time::timeout;

/// Five points, one of each kind, one of them time-tagged.
const POINTS: &str = r#"{"schema_version": 1, "points": [
    {"address": 1, "kind": "single", "value": true},
    {"address": 2, "kind": "float", "value": 1.5, "stamped": true},
    {"address": 3, "kind": "normalized", "value": 0.25},
    {"address": 4, "kind": "scaled", "value": -700},
    {"address": 5, "kind": "double", "value": "on"}
]}"#;

/// A points file of one test, removed when the test ends.
struct PointsFile(PathBuf);

impl PointsFile {
    fn new(name: &str, text: &str) -> Self {
        let path = std::env::temp_dir().join(format!("igs-s2-{}-{name}.json", std::process::id()));
        std::fs::write(&path, text).expect("the points file is written");
        Self(path)
    }
}

impl Drop for PointsFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// A runtime running as a station, with its addresses and the way to stop it.
struct Running {
    control: SocketAddr,
    station: SocketAddr,
    stop: oneshot::Sender<()>,
    serving: JoinHandle<std::io::Result<igs_iec104_runtime::runtime::Stopped>>,
}

impl Running {
    async fn start(points: &PointsFile) -> Self {
        let token = TokenFile::new("s2");
        let config = Config {
            role: Role::Server {
                listen: "127.0.0.1:0".parse().expect("address"),
                points: points.0.clone(),
                selection_timeout: std::time::Duration::from_secs(30),
            },
            common_address: 1,
            control: DEFAULT_CONTROL,
            event_capacity: DEFAULT_EVENT_CAPACITY,
            parameters: Parameters::default(),
            control_token: token.0.clone(),
        };
        let runtime = Runtime::bind(config).await.expect("the runtime binds");
        let control = runtime.control_address();
        let station = runtime.station_address().expect("the runtime is a station");
        let (stop, stopped) = oneshot::channel::<()>();
        let serving = tokio::spawn(runtime.serve(async {
            let _ = stopped.await;
        }));
        wait_for(control, "/v1/ready", |body| body["role"] == "station").await;
        Self {
            control,
            station,
            stop,
            serving,
        }
    }

    async fn stop(self) {
        let _ = self.stop.send(());
        self.serving
            .await
            .expect("the runtime task ends")
            .expect("the control API stops without an error");
    }
}

/// The value of the single-point information at `address` in an ASDU, if the ASDU carries it.
fn single_at(asdu: &Asdu, address: u32) -> Option<bool> {
    let Body::M_SP_NA_1(objects) = &asdu.body else {
        return None;
    };
    match objects {
        Objects::Individual(items) => items
            .iter()
            .find(|item| item.address.value() == address)
            .map(|item| item.value.on),
        Objects::Sequence {
            address: first,
            values,
        } => {
            let index = address.checked_sub(first.value())?;
            values.get(index as usize).map(|value| value.on)
        }
    }
}

/// Waits until the upstream client's data transfer is started.
async fn wait_started(client: &mut Client) {
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
    .expect("the transfer starts within the patience of the test");
}

/// Interrogates the station and returns the single-point value at `address` in the answer.
async fn interrogate(client: &mut Client, address: u32) -> Option<bool> {
    client
        .interrogate(Qoi::STATION)
        .await
        .expect("the interrogation is queued");
    timeout(PATIENCE, async {
        let mut value = None;
        while let Some(event) = client.next_event().await {
            if let Event::Delivery(Delivery::Asdu(asdu)) = event {
                if asdu.cot.cause() == 10 {
                    return value;
                }
                value = value.or_else(|| single_at(&asdu, address));
            }
        }
        None
    })
    .await
    .expect("the interrogation ends within the patience of the test")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_upstream_client_reads_a_point_updated_through_the_control_api() {
    let points = PointsFile::new("update", POINTS);
    let running = Running::start(&points).await;
    let mut client = Client::connect(ClientConfig::new(running.station, CommonAddress::new(1)));
    client
        .start_data_transfer()
        .await
        .expect("the transfer request is queued");
    wait_started(&mut client).await;

    assert_eq!(interrogate(&mut client, 1).await, Some(true));

    let (status, body) = post(running.control, "/v1/points/1", &json!({"value": false})).await;
    assert_eq!(status, 200, "the update is accepted: {body}");
    assert_eq!(body["value"], false);

    assert_eq!(interrogate(&mut client, 1).await, Some(false));

    drop(client);
    running.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_station_lists_its_points_and_refuses_bad_updates() {
    let points = PointsFile::new("list", POINTS);
    let running = Running::start(&points).await;
    let control = running.control;

    let (status, ready) = get(control, "/v1/ready").await;
    assert_eq!(status, 200);
    assert_eq!(ready["role"], "station");
    assert_eq!(ready["listen_address"], running.station.to_string());
    assert_eq!(ready["points"], 5);

    let (status, body) = get(control, "/v1/points").await;
    assert_eq!(status, 200);
    let listed = body["points"].as_array().expect("the points are a list");
    assert_eq!(listed.len(), 5);
    assert_eq!(listed[1]["kind"], "float");
    assert_eq!(listed[1]["stamped"], true);
    assert_eq!(listed[4]["value"], "on");

    let (status, body) = post(control, "/v1/points/2", &json!({"value": true})).await;
    assert_eq!(status, 400, "a boolean is not a float: {body}");
    assert!(body["error"]
        .as_str()
        .is_some_and(|error| error.contains("point 2")));

    let (status, _) = post(control, "/v1/points/99", &json!({"value": true})).await;
    assert_eq!(status, 404);

    let (status, _) = send(control, "POST", "/v1/transfer/start", None).await;
    assert_eq!(status, 409, "a station does not start a transfer");

    running.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_control_api_refuses_a_request_without_the_control_token() {
    let points = PointsFile::new("auth", POINTS);
    let running = Running::start(&points).await;
    let control = running.control;

    let (status, body) = send_with(control, "GET", "/v1/ready", None, None).await;
    assert_eq!(status, 401, "no token: {body}");
    assert!(body["error"].is_string());

    let wrong = "Bearer wrong-token-0123456789abcdefghijklmnop";
    let (status, _) = send_with(control, "GET", "/v1/ready", None, Some(wrong)).await;
    assert_eq!(status, 401, "a wrong token is refused");

    // The check comes before the routing: an unknown path is not revealed to a caller without it.
    let (status, _) = send_with(control, "GET", "/v1/no-such-route", None, None).await;
    assert_eq!(status, 401);

    running.stop().await;
}

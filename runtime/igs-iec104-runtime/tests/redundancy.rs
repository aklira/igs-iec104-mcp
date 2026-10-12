//! A client of a redundancy group, through the runtime's own configuration (IMPLEMENTATION.md
//! §10; task V2a). Two stations serve the same point. The client starts the data transfer of one
//! of them. When that station stops, the client switches to the other: the switchover is an event
//! of the client, the transfer runs on the other station, and the client's image holds its value.

mod common;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use common::{get, has_event, wait_for, TokenFile};
use igs_iec104_link::Parameters;
use igs_iec104_runtime::config::{Config, Role, DEFAULT_CONTROL};
use igs_iec104_runtime::runtime::{Runtime, Stopped};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

/// One point, which the stations serve and the client reads after the switchover.
const POINTS: &str = r#"{"schema_version": 1, "points": [
    {"address": 100, "kind": "float", "value": 12.5}
]}"#;

/// A points file of one test, removed when the test ends.
struct PointsFile(PathBuf);

impl PointsFile {
    fn new(name: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "igs-v2a-{}-{unique}-{name}.json",
            std::process::id()
        ));
        std::fs::write(&path, POINTS).expect("the points file is written");
        Self(path)
    }
}

impl Drop for PointsFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// A runtime running in the background, with its addresses and the way to stop it.
struct Running {
    control: SocketAddr,
    address: Option<SocketAddr>,
    stop: oneshot::Sender<()>,
    serving: JoinHandle<std::io::Result<Stopped>>,
}

impl Running {
    async fn start(config: Config) -> Self {
        let runtime = Runtime::bind(config).await.expect("the runtime binds");
        let control = runtime.control_address();
        let address = runtime.station_address();
        let (stop, stopped) = oneshot::channel::<()>();
        let serving = tokio::spawn(runtime.serve(async {
            let _ = stopped.await;
        }));
        Self {
            control,
            address,
            stop,
            serving,
        }
    }

    async fn stop(self) -> Stopped {
        let _ = self.stop.send(());
        self.serving
            .await
            .expect("the runtime task ends")
            .expect("the control API stops without an error")
    }
}

fn config(role: Role, token: &TokenFile) -> Config {
    Config {
        role,
        common_address: 1,
        control: DEFAULT_CONTROL,
        event_capacity: 1024,
        parameters: Parameters::default(),
        control_token: token.0.clone(),
        tls: None,
    }
}

async fn station(points: &PointsFile, token: &TokenFile) -> Running {
    let running = Running::start(config(
        Role::Server {
            listen: "127.0.0.1:0".parse().expect("address"),
            points: points.0.clone(),
            selection_timeout: Duration::from_secs(30),
        },
        token,
    ))
    .await;
    wait_for(running.control, "/v1/ready", |body| {
        body["role"] == "station"
    })
    .await;
    running
}

#[tokio::test]
async fn a_group_switches_to_the_other_station_when_the_started_one_stops() {
    let points = PointsFile::new("switch");
    let token = TokenFile::new("switch");
    let first = station(&points, &token).await;
    let second = station(&points, &token).await;
    let addresses = vec![
        first.address.expect("the first station listens"),
        second.address.expect("the second station listens"),
    ];
    let client = Running::start(config(Role::Client { connect: addresses }, &token)).await;

    // The group starts the data transfer of one station, and interrogates it.
    let ready = wait_for(client.control, "/v1/ready", |body| {
        body["transfer"] == "started"
    })
    .await;
    assert_eq!(ready["endpoints"], 2);
    let started = ready["started_endpoint"]
        .as_u64()
        .expect("a station is started") as usize;
    let other = 1 - started;
    let mut stations = vec![first, second];
    let stopping = stations.remove(started);
    stopping.stop().await;

    // The client switches to the other station, which starts its transfer.
    let ready = wait_for(client.control, "/v1/ready", |body| {
        body["started_endpoint"] == other as u64 && body["transfer"] == "started"
    })
    .await;
    assert_eq!(ready["connection"], "up");

    let (_, events) = get(client.control, "/v1/events").await;
    assert!(
        has_event(&events, |event| {
            event["kind"] == "switched"
                && event["from"] == started as u64
                && event["to"] == other as u64
        }),
        "the switchover is an event of the client: {events}"
    );
    assert!(
        has_event(&events, |event| {
            event["kind"] == "disconnected" && event["endpoint"] == started as u64
        }),
        "the lost station's disconnection names its station: {events}"
    );

    // The interrogation after the switchover brings the station's value into the client's image.
    let image = wait_for(client.control, "/v1/image/points", |body| {
        body["points"]
            .as_array()
            .is_some_and(|points| points.iter().any(|p| p["address"] == 100))
    })
    .await;
    assert_eq!(image["points"][0]["value"], 12.5);

    client.stop().await;
    // The station that stayed is the one left after the stopped station was removed.
    let remaining = stations.remove(0);
    remaining.stop().await;
}

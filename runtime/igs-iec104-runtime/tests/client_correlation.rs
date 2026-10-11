//! Task C2: the client's requests and their verdicts, against real peers.
//!
//! Two kinds of peer are used. The upstream server answers commands and interrogations, so that the
//! confirmed, refused, and completed verdicts are reached. A silent peer answers the start of the
//! data transfer and then nothing else, so that the timed out, cancelled, and indeterminate verdicts
//! are reached. The rules themselves are unit-tested in `correlation`.

mod common;

use std::net::SocketAddr;

use common::{get, has_event, post, send, wait_for, TokenFile};
use igs_iec104::process_image::PointValue;
use igs_iec104::server::{Handler, Operation, Refusal, Server, ServerConfig};
use igs_iec104_codec::elements::{QualityFlags, Siq};
use igs_iec104_codec::header::CommonAddress;
use igs_iec104_codec::header::InformationObjectAddress;
use igs_iec104_runtime::config::{Config, Role, DEFAULT_CONTROL, DEFAULT_EVENT_CAPACITY};
use igs_iec104_runtime::runtime::Runtime;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

/// The one point of the upstream station, and the only address that accepts commands.
const POINT: u32 = 1;

/// Accepts the commands to `POINT` and refuses the others as unknown addresses (cause 47).
struct AcceptPointOne;

impl Handler for AcceptPointOne {
    fn select(&self, operation: &Operation) -> Result<(), Refusal> {
        judge(operation)
    }

    fn execute(&self, operation: &Operation) -> Result<(), Refusal> {
        judge(operation)
    }
}

fn judge(operation: &Operation) -> Result<(), Refusal> {
    if operation.address.value() == POINT {
        Ok(())
    } else {
        Err(Refusal::UnknownAddress)
    }
}

/// An upstream station with one single point at `POINT`, on a port it chooses.
async fn upstream_station() -> SocketAddr {
    let bind: SocketAddr = "127.0.0.1:0".parse().expect("address");
    let server = Server::bind(
        ServerConfig::new(bind, CommonAddress::new(1)),
        AcceptPointOne,
    )
    .await
    .expect("the station binds");
    let address = server.local_addr().expect("the station has an address");
    let point = InformationObjectAddress::new(POINT).expect("an address in range");
    let on = PointValue::Single(Siq {
        on: true,
        quality: QualityFlags::default(),
    });
    server
        .handle()
        .add_point(point, on, false)
        .expect("the point is added");
    tokio::spawn(server.run());
    address
}

/// A silent peer: it answers the start of the data transfer, writes what it receives, and answers
/// nothing else. The connection closes when `close` fires.
async fn silent_station(close: oneshot::Receiver<()>) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("the peer binds");
    let address = listener.local_addr().expect("the peer has an address");
    tokio::spawn(async move {
        let Ok((mut stream, _)) = listener.accept().await else {
            return;
        };
        // STARTDT act is a six-octet control frame (IEC 60870-5-104 §5.3); STARTDT con answers it.
        let mut start = [0_u8; 6];
        if stream.read_exact(&mut start).await.is_err() {
            return;
        }
        let _ = stream
            .write_all(&[0x68, 0x04, 0x0B, 0x00, 0x00, 0x00])
            .await;
        let mut received = [0_u8; 512];
        tokio::select! {
            _ = close => {}
            () = async {
                while matches!(stream.read(&mut received).await, Ok(read) if read > 0) {}
            } => {}
        }
    });
    address
}

/// A client runtime, running, with its control address and the way to stop it.
struct Client {
    control: SocketAddr,
    stop: oneshot::Sender<()>,
    serving: JoinHandle<std::io::Result<()>>,
}

impl Client {
    async fn start(station: SocketAddr, token: &TokenFile) -> Self {
        let config = Config {
            role: Role::Client { connect: station },
            common_address: 1,
            control: DEFAULT_CONTROL,
            event_capacity: DEFAULT_EVENT_CAPACITY,
            control_token: token.0.clone(),
        };
        let runtime = Runtime::bind(config).await.expect("the runtime binds");
        let control = runtime.control_address();
        let (stop, stopped) = oneshot::channel::<()>();
        let serving = tokio::spawn(runtime.serve(async {
            let _ = stopped.await;
        }));
        wait_for(control, "/v1/ready", |body| body["connection"] == "up").await;
        Self {
            control,
            stop,
            serving,
        }
    }

    async fn start_transfer(&self) {
        let (status, _) = post(self.control, "/v1/transfer/start", &json!(null)).await;
        assert_eq!(status, 202);
        wait_for(self.control, "/v1/events", |body| {
            has_event(body, |event| {
                event["kind"] == "transfer" && event["state"] == "started"
            })
        })
        .await;
    }

    async fn stop(self) {
        let _ = self.stop.send(());
        self.serving
            .await
            .expect("the runtime task ends")
            .expect("the control API stops without an error");
    }
}

/// Sends a single command, and returns the identity of its request.
async fn command(client: &Client, address: u32, select: bool, timeout_ms: u64) -> u64 {
    let body = json!({"address": address, "on": true, "select": select, "timeout_ms": timeout_ms});
    let (status, answer) = post(client.control, "/v1/commands/single", &body).await;
    assert_eq!(status, 202, "the command is queued: {answer}");
    answer["request"]
        .as_u64()
        .expect("the request has an identity")
}

/// Waits until the request record satisfies `reached`, and returns it.
async fn request_until(client: &Client, id: u64, reached: impl Fn(&Value) -> bool) -> Value {
    wait_for_request(client.control, id, reached).await
}

async fn wait_for_request(control: SocketAddr, id: u64, reached: impl Fn(&Value) -> bool) -> Value {
    let path = format!("/v1/requests/{id}");
    wait_for(control, &path, reached).await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_station_confirms_a_selection_an_execution_and_an_interrogation() {
    let token = TokenFile::new("c2-accept");
    let client = Client::start(upstream_station().await, &token).await;
    client.start_transfer().await;

    let select = command(&client, POINT, true, 5_000).await;
    let record = request_until(&client, select, |body| body["outcome"] == "confirmed").await;
    assert_eq!(record["kind"], "single_command_select");
    assert_eq!(record["stage"], "ended");
    assert_eq!(record["sent"], true);
    assert_eq!(record["generation"], 1);

    let execute = command(&client, POINT, false, 5_000).await;
    let record = request_until(&client, execute, |body| body["outcome"] == "confirmed").await;
    assert_eq!(record["kind"], "single_command_execute");

    let (status, answer) = post(client.control, "/v1/interrogation/general", &json!(null)).await;
    assert_eq!(status, 202);
    let interrogation = answer["request"].as_u64().expect("an identity");
    request_until(&client, interrogation, |body| {
        body["outcome"] == "confirmed"
    })
    .await;

    let (status, image) = get(client.control, "/v1/image/points").await;
    assert_eq!(status, 200);
    let point = image["points"]
        .as_array()
        .and_then(|points| points.iter().find(|point| point["address"] == POINT))
        .expect("the station's point is in the image");
    assert_eq!(point["value"], true);
    assert_eq!(point["cause"], 20, "the value came with the interrogation");
    assert_eq!(point["generation"], 1);

    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_command_to_an_unknown_address_is_refused_with_cause_47() {
    let token = TokenFile::new("c2-refused");
    let client = Client::start(upstream_station().await, &token).await;
    client.start_transfer().await;

    // The station refuses a selection of an address it does not have: cause 47, as a negative
    // confirmation (IEC 60870-5-104 §8.9, and the station's own rule).
    let id = command(&client, 2, true, 5_000).await;
    let record = request_until(&client, id, |body| body["outcome"] == "refused").await;
    assert_eq!(record["cause"], 47);
    assert_eq!(record["negative"], true);

    let (_, events) = get(client.control, "/v1/events").await;
    assert!(has_event(&events, |event| {
        event["kind"] == "verdict" && event["request"] == id && event["outcome"] == "refused"
    }));
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_execution_without_its_selection_is_refused_as_the_station_requires() {
    let token = TokenFile::new("c2-no-select");
    let client = Client::start(upstream_station().await, &token).await;
    client.start_transfer().await;

    // Select-before-operate: an execution needs a kept selection of the same address (§7).
    let id = command(&client, POINT, false, 5_000).await;
    let record = request_until(&client, id, |body| body["outcome"] == "refused").await;
    assert_eq!(record["cause"], 7);
    assert_eq!(record["negative"], true);
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_command_is_refused_while_the_transfer_is_not_started() {
    let token = TokenFile::new("c2-not-started");
    let client = Client::start(upstream_station().await, &token).await;

    let body = json!({"address": POINT, "on": true, "select": true});
    let (status, answer) = post(client.control, "/v1/commands/single", &body).await;
    assert_eq!(status, 409, "{answer}");
    assert!(answer["error"]
        .as_str()
        .is_some_and(|error| error.contains("not started")));
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_command_the_station_never_answers_times_out_after_it_was_sent() {
    let token = TokenFile::new("c2-timeout");
    let (_keep_open, close) = oneshot::channel::<()>();
    let client = Client::start(silent_station(close).await, &token).await;
    client.start_transfer().await;

    let id = command(&client, POINT, false, 200).await;
    let record = request_until(&client, id, |body| body["outcome"] == "timed_out").await;
    assert_eq!(
        record["sent"], true,
        "the command was written before it timed out"
    );
    assert_eq!(record["generation"], 1);
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_command_cut_off_by_the_connection_is_indeterminate_and_not_resent() {
    let token = TokenFile::new("c2-indeterminate");
    let (cut, close) = oneshot::channel::<()>();
    let client = Client::start(silent_station(close).await, &token).await;
    client.start_transfer().await;

    let id = command(&client, POINT, false, 30_000).await;
    request_until(&client, id, |body| body["sent"] == true).await;
    let _ = cut.send(());

    let record = request_until(&client, id, |body| body["outcome"] == "indeterminate").await;
    assert_eq!(record["generation"], 1);
    let (_, events) = get(client.control, "/v1/events").await;
    assert!(has_event(&events, |event| {
        event["kind"] == "verdict"
            && event["request"] == id
            && event["outcome"] == "indeterminate"
            && event["transmitted_generation"] == 1
    }));
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_cancelled_command_says_it_was_already_written_and_cannot_be_cancelled_twice() {
    let token = TokenFile::new("c2-cancel");
    let (_keep_open, close) = oneshot::channel::<()>();
    let client = Client::start(silent_station(close).await, &token).await;
    client.start_transfer().await;

    let id = command(&client, POINT, false, 30_000).await;
    request_until(&client, id, |body| body["sent"] == true).await;

    let (status, record) = post(
        client.control,
        &format!("/v1/requests/{id}/cancel"),
        &json!(null),
    )
    .await;
    assert_eq!(status, 200, "{record}");
    assert_eq!(record["outcome"], "cancelled");
    assert_eq!(record["sent"], true);

    let (status, again) = post(
        client.control,
        &format!("/v1/requests/{id}/cancel"),
        &json!(null),
    )
    .await;
    assert_eq!(status, 409, "{again}");
    client.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_unknown_request_is_not_found() {
    let token = TokenFile::new("c2-unknown");
    let client = Client::start(upstream_station().await, &token).await;
    let (status, _) = send(client.control, "GET", "/v1/requests/999", None).await;
    assert_eq!(status, 404);
    client.stop().await;
}

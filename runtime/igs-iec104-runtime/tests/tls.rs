//! A secured station and a secured client, through the runtime's own configuration (IMPLEMENTATION.md
//! §6, §9; task V1a). The certificates are made for each test with the `openssl` command line: a
//! root, a station and a client certificate signed by it, and a second root that signs nothing the
//! station or the client presents.

mod common;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use common::{get, has_event, post, wait_for, TokenFile};
use igs_iec104_link::Parameters;
use igs_iec104_runtime::config::{Config, Role, TlsFiles, DEFAULT_CONTROL};
use igs_iec104_runtime::runtime::{BindError, Runtime, Stopped};
use igs_iec104_runtime::tls::TlsSetupError;
use serde_json::json;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

/// The name the station's certificate is issued to, and the client checks it against.
const STATION_NAME: &str = "station.test";

/// The points of the station: one value, which a client interrogates.
const POINTS: &str = r#"{"schema_version": 1, "points": [
    {"address": 100, "kind": "float", "value": 12.5}
]}"#;

/// The certificates and keys of one test, in a directory removed when the test ends.
struct Pki {
    dir: PathBuf,
}

impl Pki {
    fn new(name: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("igs-v1a-{}-{unique}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("the directory is made");
        let pki = Self { dir };
        pki.root("root", "test-root");
        pki.other_root();
        pki.leaf("station", "station.test");
        pki.leaf("client", "client.test");
        pki.intermediate();
        pki.leaf_signed_by("chained", "station.test", "inter");
        pki
    }

    fn openssl(&self, args: &[&str]) {
        let output = Command::new("openssl")
            .args(args)
            .current_dir(&self.dir)
            .output()
            .expect("the openssl command runs: install it to run these tests");
        assert!(
            output.status.success(),
            "openssl {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn root(&self, stem: &str, subject: &str) {
        self.openssl(&[
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            &format!("{stem}.key"),
            "-out",
            &format!("{stem}.pem"),
            "-subj",
            &format!("/CN={subject}"),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ]);
        self.der(stem);
    }

    fn other_root(&self) {
        self.root("other", "other-root");
    }

    /// A certificate for `stem`, signed by the root, with the name and the address the tests use.
    fn leaf(&self, stem: &str, name: &str) {
        self.leaf_signed_by(stem, name, "root");
    }

    /// A certificate for `stem`, signed by the authority `issuer` (`root` or `inter`).
    fn leaf_signed_by(&self, stem: &str, name: &str, issuer: &str) {
        self.openssl(&[
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            &format!("{stem}.key"),
            "-out",
            &format!("{stem}.csr"),
            "-subj",
            &format!("/CN={name}"),
        ]);
        std::fs::write(
            self.dir.join(format!("{stem}.ext")),
            format!(
                "subjectAltName=DNS:{name},IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\n"
            ),
        )
        .expect("the extensions are written");
        self.openssl(&[
            "x509",
            "-req",
            "-in",
            &format!("{stem}.csr"),
            "-CA",
            &format!("{issuer}.pem"),
            "-CAkey",
            &format!("{issuer}.key"),
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            &format!("{stem}.ext"),
            "-out",
            &format!("{stem}.pem"),
        ]);
        self.der(stem);
    }

    /// The certificate and the key of `stem` in DER: the form the runtime reads.
    fn der(&self, stem: &str) {
        self.openssl(&[
            "x509",
            "-in",
            &format!("{stem}.pem"),
            "-outform",
            "DER",
            "-out",
            &format!("{stem}.der"),
        ]);
        self.openssl(&[
            "pkcs8",
            "-topk8",
            "-nocrypt",
            "-in",
            &format!("{stem}.key"),
            "-outform",
            "DER",
            "-out",
            &format!("{stem}.key.der"),
        ]);
    }

    /// An intermediate authority, signed by the root, able to sign a station's certificate.
    fn intermediate(&self) {
        self.openssl(&[
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "inter.key",
            "-out",
            "inter.csr",
            "-subj",
            "/CN=test-intermediate",
        ]);
        std::fs::write(
            self.dir.join("inter.ext"),
            "basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n",
        )
        .expect("the extensions are written");
        self.openssl(&[
            "x509",
            "-req",
            "-in",
            "inter.csr",
            "-CA",
            "root.pem",
            "-CAkey",
            "root.key",
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            "inter.ext",
            "-out",
            "inter.pem",
        ]);
        self.der("inter");
    }

    /// The files of a runtime with `certificate` and `key`, trusting the root `trust`.
    fn files(&self, certificate: &str, trust: &str, server_name: Option<&str>) -> TlsFiles {
        self.chained(certificate, trust, server_name, &[])
    }

    /// The files of a runtime whose certificate is sent with the intermediates `intermediates`.
    fn chained(
        &self,
        certificate: &str,
        trust: &str,
        server_name: Option<&str>,
        intermediates: &[&str],
    ) -> TlsFiles {
        TlsFiles {
            certificate: self.dir.join(format!("{certificate}.der")),
            key: self.dir.join(format!("{certificate}.key.der")),
            trust: self.dir.join(format!("{trust}.der")),
            server_name: server_name.map(str::to_owned),
            intermediates: intermediates
                .iter()
                .map(|stem| self.dir.join(format!("{stem}.der")))
                .collect(),
        }
    }
}

impl Drop for Pki {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// A points file of one test, removed when the test ends.
struct PointsFile(PathBuf);

impl PointsFile {
    fn new(pki: &Pki) -> Self {
        let path = pki.dir.join("points.json");
        std::fs::write(&path, POINTS).expect("the points file is written");
        Self(path)
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

fn config(role: Role, token: &TokenFile, tls: Option<TlsFiles>) -> Config {
    Config {
        role,
        common_address: 1,
        control: DEFAULT_CONTROL,
        event_capacity: 1024,
        parameters: Parameters::default(),
        control_token: token.0.clone(),
        tls,
    }
}

async fn station(pki: &Pki, token: &TokenFile, tls: Option<TlsFiles>) -> Running {
    let points = PointsFile::new(pki);
    let running = Running::start(config(
        Role::Server {
            listen: "127.0.0.1:0".parse().expect("address"),
            points: points.0.clone(),
            selection_timeout: Duration::from_secs(30),
        },
        token,
        tls,
    ))
    .await;
    wait_for(running.control, "/v1/ready", |body| {
        body["role"] == "station"
    })
    .await;
    running
}

async fn client(station: SocketAddr, token: &TokenFile, tls: Option<TlsFiles>) -> Running {
    let running = Running::start(config(
        Role::Client {
            connect: vec![station],
        },
        token,
        tls,
    ))
    .await;
    running
}

#[tokio::test]
async fn a_secured_client_reaches_a_secured_station_and_starts_its_transfer() {
    let pki = Pki::new("accepted");
    let token = TokenFile::new("accepted");
    let station = station(&pki, &token, Some(pki.files("station", "root", None))).await;
    let address = station.address.expect("the station listens");
    let client = client(
        address,
        &token,
        Some(pki.files("client", "root", Some(STATION_NAME))),
    )
    .await;

    wait_for(client.control, "/v1/ready", |body| {
        body["connection"] == "up"
    })
    .await;
    let (status, _) = post(client.control, "/v1/transfer/start", &json!({})).await;
    assert_eq!(status, 202);
    wait_for(client.control, "/v1/ready", |body| {
        body["transfer"] == "started"
    })
    .await;
    // The station records the handshake it accepted, as a notice.
    wait_for(station.control, "/v1/station/events", |body| {
        has_event(body, |event| {
            event["kind"] == "security" && event["severity"] == "notice"
        })
    })
    .await;

    client.stop().await;
    station.stop().await;
}

#[tokio::test]
async fn a_client_that_trusts_another_authority_is_refused_and_says_why() {
    let pki = Pki::new("refused");
    let token = TokenFile::new("refused");
    let station = station(&pki, &token, Some(pki.files("station", "root", None))).await;
    let address = station.address.expect("the station listens");
    let client = client(
        address,
        &token,
        Some(pki.files("client", "other", Some(STATION_NAME))),
    )
    .await;

    // The handshake fails, so the connection never comes up, and the client records the failure
    // with the reason the TLS backend gave.
    let body = wait_for(client.control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "connect_failed")
    })
    .await;
    let (_, ready) = get(client.control, "/v1/ready").await;
    assert_ne!(
        ready["connection"], "up",
        "a refused handshake brings no connection"
    );
    let failure = body["events"]
        .as_array()
        .and_then(|events| {
            events
                .iter()
                .find(|event| event["kind"] == "connect_failed")
        })
        .expect("the failure is recorded");
    assert!(
        failure["error"]
            .as_str()
            .is_some_and(|text| text.contains("handshake")),
        "the reason names the handshake: {failure}"
    );

    client.stop().await;
    station.stop().await;
}

#[tokio::test]
async fn a_runtime_whose_tls_files_are_missing_does_not_start() {
    let pki = Pki::new("missing");
    let token = TokenFile::new("missing");
    let points = PointsFile::new(&pki);
    let mut files = pki.files("client", "root", None);
    files.key = pki.dir.join("absent.key.der");
    let result = Runtime::bind(config(
        Role::Server {
            listen: "127.0.0.1:0".parse().expect("address"),
            points: points.0.clone(),
            selection_timeout: Duration::from_secs(30),
        },
        &token,
        Some(files),
    ))
    .await;
    match result {
        Err(BindError::Tls(TlsSetupError::File { path, .. })) => {
            assert!(path.ends_with("absent.key.der"));
        }
        other => panic!("expected a TLS file error, got {:?}", other.map(|_| ())),
    }
}

#[tokio::test]
async fn a_station_whose_certificate_comes_with_its_intermediate_is_accepted() {
    let pki = Pki::new("chain-accepted");
    let token = TokenFile::new("chain-accepted");
    let station = station(
        &pki,
        &token,
        Some(pki.chained("chained", "root", None, &["inter"])),
    )
    .await;
    let address = station.address.expect("the station listens");
    let client = client(
        address,
        &token,
        Some(pki.files("client", "root", Some(STATION_NAME))),
    )
    .await;

    wait_for(client.control, "/v1/ready", |body| {
        body["connection"] == "up"
    })
    .await;

    client.stop().await;
    station.stop().await;
}

#[tokio::test]
async fn a_station_that_sends_no_intermediate_is_refused_by_the_client() {
    let pki = Pki::new("chain-missing");
    let token = TokenFile::new("chain-missing");
    let station = station(
        &pki,
        &token,
        Some(pki.chained("chained", "root", None, &[])),
    )
    .await;
    let address = station.address.expect("the station listens");
    let client = client(
        address,
        &token,
        Some(pki.files("client", "root", Some(STATION_NAME))),
    )
    .await;

    wait_for(client.control, "/v1/events", |body| {
        has_event(body, |event| event["kind"] == "connect_failed")
    })
    .await;
    let (_, ready) = get(client.control, "/v1/ready").await;
    assert_ne!(
        ready["connection"], "up",
        "the issuer is missing from the chain"
    );

    client.stop().await;
    station.stop().await;
}

#[tokio::test]
async fn a_station_that_does_not_trust_the_client_records_the_refusal_and_accepts_nothing() {
    let pki = Pki::new("refused-by-station");
    let token = TokenFile::new("refused-by-station");
    // The station trusts only the other root, so the client's certificate, signed by the root, has
    // no trust anchor on the station's side.
    let station = station(&pki, &token, Some(pki.files("station", "other", None))).await;
    let address = station.address.expect("the station listens");
    let client = client(
        address,
        &token,
        Some(pki.files("client", "root", Some(STATION_NAME))),
    )
    .await;

    let body = wait_for(station.control, "/v1/station/events", |body| {
        has_event(body, |event| {
            event["kind"] == "security" && event["severity"] == "alarm"
        })
    })
    .await;
    let alarm = body["events"]
        .as_array()
        .and_then(|events| events.iter().find(|event| event["severity"] == "alarm"))
        .expect("the alarm is recorded");
    assert_eq!(
        alarm["mnemonic"], "TLS_NO_CA_MATCH",
        "the reason is named: {alarm}"
    );
    assert!(
        !has_event(&body, |event| event["kind"] == "connected"),
        "the station accepted no connection: {body}"
    );

    client.stop().await;
    station.stop().await;
}

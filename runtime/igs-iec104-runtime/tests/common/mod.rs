//! Helpers of the integration tests: one HTTP request to the control API, the control token, and
//! polling.

#![allow(dead_code)]

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{sleep, timeout};

/// The longest a test waits for the runtime to reach a state.
pub const PATIENCE: Duration = Duration::from_secs(10);

/// The control token of the tests. It has the form the runtime accepts.
pub const TOKEN: &str = "C1-test-token-0123456789abcdefghijklmn";

/// A token file of one test, removed when the test ends.
pub struct TokenFile(pub PathBuf);

impl TokenFile {
    /// A new token file. Its name is unique within the process, so that tests running at the same
    /// time do not share a file.
    pub fn new(name: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("igs-token-{}-{unique}-{name}", std::process::id()));
        std::fs::write(&path, format!("{TOKEN}\n")).expect("the token file is written");
        Self(path)
    }
}

impl Drop for TokenFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// One HTTP request to the control API with the control token, and an optional JSON body. It
/// returns the status code and the JSON body.
pub async fn send(
    control: SocketAddr,
    method: &str,
    path: &str,
    body: Option<&Value>,
) -> (u16, Value) {
    send_with(
        control,
        method,
        path,
        body,
        Some(&format!("Bearer {TOKEN}")),
    )
    .await
}

/// One HTTP request to the control API with the `Authorization` header given (or none).
pub async fn send_with(
    control: SocketAddr,
    method: &str,
    path: &str,
    body: Option<&Value>,
    authorization: Option<&str>,
) -> (u16, Value) {
    let mut stream = TcpStream::connect(control)
        .await
        .expect("the control API accepts");
    let payload = body.map(Value::to_string).unwrap_or_default();
    let header = authorization
        .map(|value| format!("Authorization: {value}\r\n"))
        .unwrap_or_default();
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {control}\r\n{header}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
        payload.len()
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

/// One GET request, without a body.
pub async fn get(control: SocketAddr, path: &str) -> (u16, Value) {
    send(control, "GET", path, None).await
}

/// One POST request, with a JSON body.
pub async fn post(control: SocketAddr, path: &str, body: &Value) -> (u16, Value) {
    send(control, "POST", path, Some(body)).await
}

/// Polls `path` until `reached` accepts its body, and returns that body.
pub async fn wait_for(control: SocketAddr, path: &str, reached: impl Fn(&Value) -> bool) -> Value {
    timeout(PATIENCE, async {
        loop {
            let (_, body) = get(control, path).await;
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
pub fn has_event(body: &Value, matches: impl Fn(&Value) -> bool) -> bool {
    body["events"]
        .as_array()
        .is_some_and(|events| events.iter().any(matches))
}

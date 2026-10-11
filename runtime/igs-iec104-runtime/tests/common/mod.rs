//! Helpers of the integration tests: one HTTP request to the control API, and polling.

#![allow(dead_code)]

use std::net::SocketAddr;
use std::time::Duration;

use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{sleep, timeout};

/// The longest a test waits for the runtime to reach a state.
pub const PATIENCE: Duration = Duration::from_secs(10);

/// One HTTP request to the control API, with an optional JSON body. It returns the status code
/// and the JSON body.
pub async fn send(
    control: SocketAddr,
    method: &str,
    path: &str,
    body: Option<&Value>,
) -> (u16, Value) {
    let mut stream = TcpStream::connect(control)
        .await
        .expect("the control API accepts");
    let payload = body.map(Value::to_string).unwrap_or_default();
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {control}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
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

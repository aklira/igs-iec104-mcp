//! The runtime as a whole: the control listener, the client, and the state they share.
//!
//! The control API has its own listener, separate from the IEC 60870-5-104 traffic (§3). It
//! binds the address it is given, and the runtime reports the address it got (§6).

use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::sync::Arc;

use igs_iec104::Client;
use tokio::net::TcpListener;
use tokio::sync::mpsc;

use crate::client_role::{self, Recorder, Request};
use crate::config::Config;

/// Requests waiting for the client. A full queue makes a request wait for its turn.
const REQUEST_QUEUE: usize = 16;

/// What the control API shares with the client's owner.
#[derive(Debug)]
pub struct Shared {
    /// The events and the status.
    pub recorder: Arc<Recorder>,
    /// The requests to the client.
    pub requests: mpsc::Sender<Request>,
    /// The address the control API listens on.
    pub control: SocketAddr,
}

/// A runtime that is bound and not yet serving. Binding first lets the caller learn the
/// control address before the runtime serves.
pub struct Runtime {
    listener: TcpListener,
    client: Client,
    requests: mpsc::Receiver<Request>,
    shared: Arc<Shared>,
}

impl Runtime {
    /// Binds the control address and starts the client. The client connects in the background.
    pub async fn bind(config: Config) -> io::Result<Self> {
        let listener = TcpListener::bind(config.control).await?;
        let control = listener.local_addr()?;
        let client = Client::connect(client_role::client_config(
            config.connect,
            config.common_address,
        ));
        let (requests_tx, requests) = mpsc::channel(REQUEST_QUEUE);
        let shared = Arc::new(Shared {
            recorder: Arc::new(Recorder::new(config.event_capacity)),
            requests: requests_tx,
            control,
        });
        Ok(Self {
            listener,
            client,
            requests,
            shared,
        })
    }

    /// The address the control API listens on.
    pub fn control_address(&self) -> SocketAddr {
        self.shared.control
    }

    /// Serves the control API and runs the client until `shutdown` completes. The client
    /// stops when the control API does.
    pub async fn serve<F>(self, shutdown: F) -> io::Result<()>
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let Self {
            listener,
            client,
            requests,
            shared,
        } = self;
        // The owner holds only the recorder, not the request sender: it ends when the control
        // API ends, because the last sender is then dropped.
        let recorder = Arc::clone(&shared.recorder);
        let owner = tokio::spawn(client_role::run(client, requests, recorder));
        let served = axum::serve(listener, crate::api::router(shared))
            .with_graceful_shutdown(shutdown)
            .await;
        owner.abort();
        served
    }
}

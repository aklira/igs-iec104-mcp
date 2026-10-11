//! The runtime's binary: prints its identity, or runs a client of one station, or a station.

#![deny(unsafe_code)]

use std::process::ExitCode;

use igs_iec104_runtime::config::{self, Config, Invocation, USAGE};
use igs_iec104_runtime::runtime::Runtime;
use tokio::signal::unix::{signal, SignalKind};

#[tokio::main]
async fn main() -> ExitCode {
    match config::parse(std::env::args().skip(1)) {
        Ok(Invocation::Banner) => {
            println!("{}", igs_iec104_runtime::banner());
            ExitCode::SUCCESS
        }
        Ok(Invocation::Usage) => {
            println!("{USAGE}");
            ExitCode::SUCCESS
        }
        Ok(Invocation::Run(config)) => run(config).await,
        Err(error) => {
            eprintln!("igs-iec104-runtime: {error}\n\n{USAGE}");
            ExitCode::from(2)
        }
    }
}

/// Completes when the runtime is asked to stop: Ctrl-C, or SIGTERM from `docker stop`. A process
/// with PID 1 in a container ignores SIGTERM unless it handles it, so it must be handled here.
async fn stop_requested() {
    let terminate = async {
        match signal(SignalKind::terminate()) {
            Ok(mut terminate) => {
                terminate.recv().await;
            }
            Err(error) => {
                tracing::error!(%error, "SIGTERM could not be watched");
                std::future::pending::<()>().await;
            }
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        () = terminate => {}
    }
}

/// Runs the role until the runtime is asked to stop.
async fn run(config: Config) -> ExitCode {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .init();
    let runtime = match Runtime::bind(config).await {
        Ok(runtime) => runtime,
        Err(error) => {
            tracing::error!(%error, "the runtime cannot start");
            return ExitCode::FAILURE;
        }
    };
    tracing::info!(control = %runtime.control_address(), "control API listening");
    if let Some(station) = runtime.station_address() {
        tracing::info!(%station, "station listening");
    }
    match runtime.serve(stop_requested()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            tracing::error!(%error, "the control API stopped with an error");
            ExitCode::FAILURE
        }
    }
}

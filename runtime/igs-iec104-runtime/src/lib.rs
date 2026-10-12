//! The IEC 60870-5-104 runtime of the test bench orchestrator (IMPLEMENTATION.md §3).
//!
//! The runtime runs one client of the upstream library against one station, and exposes a
//! control API on its own address. Task S1 covers the client role: the transfer, the general
//! interrogation, and the events of the connection. The control API is versioned in task C1.

#![deny(unsafe_code)]

pub mod api;
pub mod client_role;
pub mod config;
pub mod correlation;
pub mod events;
pub mod image;
pub mod points;
pub mod runtime;
pub mod server_role;
pub mod simulate;
pub mod station_log;
pub mod tls;
pub mod token;

use std::sync::{Mutex, MutexGuard, PoisonError};

use igs_iec104::DEFAULT_PORT;

/// The version of this runtime, as given in its manifest.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The version of the control API and event contract (`contracts/`). It changes with an incompatible
/// change of either file.
pub const CONTRACT_VERSION: &str = "1.8.0";

/// The identity line of the runtime: its name, version, and the protocol port of the library.
pub fn banner() -> String {
    format!("igs-iec104-runtime {VERSION} (IEC 60870-5-104 default port {DEFAULT_PORT})")
}

/// Locks a state shared between tasks. A task that panicked while holding the lock leaves the
/// state it wrote, which is still consistent for these records, so the lock is taken anyway.
pub(crate) fn lock<T>(state: &Mutex<T>) -> MutexGuard<'_, T> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_banner_names_the_runtime_and_its_version() {
        assert!(banner().starts_with(&format!("igs-iec104-runtime {VERSION} ")));
    }

    #[test]
    fn the_banner_gives_the_default_protocol_port() {
        assert!(banner().ends_with(&format!("port {DEFAULT_PORT})")));
    }
}

//! The IEC 60870-5-104 runtime of the test bench orchestrator (IMPLEMENTATION.md §3).
//!
//! Task B2 provides the skeleton only: the binary starts, prints its identity, and exits. The
//! client and server roles follow in tasks S1 and S2.

#![deny(unsafe_code)]

use std::process::ExitCode;

/// The version of this runtime, as given in its manifest.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The identity line printed at start: the runtime, and the protocol port of the library it is
/// built on.
fn banner() -> String {
    format!(
        "igs-iec104-runtime {VERSION} (IEC 60870-5-104 default port {})",
        igs_iec104::DEFAULT_PORT
    )
}

fn main() -> ExitCode {
    println!("{}", banner());
    ExitCode::SUCCESS
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
        assert!(banner().ends_with(&format!("port {})", igs_iec104::DEFAULT_PORT)));
    }
}

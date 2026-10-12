//! The TLS of the runtime's connections (IMPLEMENTATION.md §6, §9; task V1a).
//!
//! The runtime reads its certificate, its private key, and its trust anchor, all DER, and builds
//! the settings of the library from them. The settings are checked against the profile before any
//! connection is made, so a runtime whose TLS is not valid does not start: it never falls back to
//! a plain connection.

use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use igs_iec104::tls::events::{SecurityEvent, SecurityEvents};
use igs_iec104::tls::openssl_backend::OpensslBackend;
use igs_iec104::tls::settings::{Identity, TlsSettings, TrustAnchors};
use igs_iec104::tls::{Secure, TlsError};

use crate::config::TlsFiles;
use crate::station_log::{StationEventKind, StationLog};

/// The security events of a station's handshakes, recorded in its log as they are raised, so that
/// a refused client leaves its reason in the station's events (IMPLEMENTATION.md §9; task V1c).
pub struct StationSecurity {
    log: Arc<Mutex<StationLog>>,
}

impl StationSecurity {
    /// The receiver that records into `log`.
    pub fn new(log: Arc<Mutex<StationLog>>) -> Self {
        Self { log }
    }
}

impl SecurityEvents for StationSecurity {
    fn raise(&self, event: SecurityEvent, detail: &str) {
        crate::lock(&self.log).record(StationEventKind::Security {
            mnemonic: event.mnemonic().to_owned(),
            severity: event.severity().to_string(),
            identifier: event.identifier(),
            detail: detail.to_owned(),
        });
    }
}

/// Why the TLS files cannot secure the connections.
#[derive(Debug)]
pub enum TlsSetupError {
    /// A file cannot be read.
    File {
        /// The file.
        path: PathBuf,
        /// Why it cannot be read.
        source: io::Error,
    },
    /// The files do not make valid settings, or the backend refuses them.
    Settings(TlsError),
}

impl fmt::Display for TlsSetupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::File { path, source } => {
                write!(
                    f,
                    "the TLS file {} cannot be read: {source}",
                    path.display()
                )
            }
            Self::Settings(error) => write!(f, "the TLS files are not usable: {error}"),
        }
    }
}

impl std::error::Error for TlsSetupError {}

/// The secure wrapper of the runtime's connections, built from its TLS files. The settings of the
/// library are checked against the profile first. The chain is the certificate, then the
/// intermediates in the order they were given. The handshakes raise their security events to
/// `events`.
pub fn secure(files: &TlsFiles, events: Arc<dyn SecurityEvents>) -> Result<Secure, TlsSetupError> {
    let mut chain = vec![read(&files.certificate)?];
    for intermediate in &files.intermediates {
        chain.push(read(intermediate)?);
    }
    let key = read(&files.key)?;
    let trust = read(&files.trust)?;
    let identity = Identity::new(chain, key, events.as_ref()).map_err(TlsSetupError::Settings)?;
    let anchors = TrustAnchors::new(vec![trust]).map_err(TlsSetupError::Settings)?;
    let settings = TlsSettings::new(identity, anchors, events);
    settings.validate().map_err(TlsSetupError::Settings)?;
    let backend = OpensslBackend::new(&settings).map_err(TlsSetupError::Settings)?;
    Ok(Secure::new(backend))
}

fn read(path: &Path) -> Result<Vec<u8>, TlsSetupError> {
    std::fs::read(path).map_err(|source| TlsSetupError::File {
        path: path.to_owned(),
        source,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use igs_iec104::tls::events::Discard;

    #[test]
    fn a_missing_file_is_named_and_refused() {
        let files = TlsFiles {
            certificate: "/nonexistent/c.der".into(),
            key: "/nonexistent/k.der".into(),
            trust: "/nonexistent/t.der".into(),
            server_name: None,
            intermediates: Vec::new(),
        };
        match secure(&files, Arc::new(Discard)) {
            Err(TlsSetupError::File { path, .. }) => {
                assert_eq!(path, PathBuf::from("/nonexistent/c.der"));
            }
            other => panic!("expected a file error, got {:?}", other.map(|_| ())),
        }
    }

    #[test]
    fn an_empty_key_is_refused_before_any_connection() {
        let dir = std::env::temp_dir().join(format!("igs-v1a-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("the directory is made");
        for name in ["c.der", "k.der", "t.der"] {
            std::fs::write(dir.join(name), b"not a certificate").expect("written");
        }
        let files = TlsFiles {
            certificate: dir.join("c.der"),
            key: dir.join("k.der"),
            trust: dir.join("t.der"),
            server_name: None,
            intermediates: Vec::new(),
        };
        let result = secure(&files, Arc::new(Discard));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(matches!(result, Err(TlsSetupError::Settings(_))));
    }
}

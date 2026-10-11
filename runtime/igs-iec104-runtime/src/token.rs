//! The control token: the credential of the control API (IMPLEMENTATION.md §3).
//!
//! The controller writes a random token to a file and gives the runtime its path. The runtime reads
//! it once, at start, and requires it on every route of the control API, in the `Authorization`
//! header as `Bearer <token>`. The token is never logged or printed: its `Debug` form is redacted.

use std::fmt;
use std::io;
use std::path::Path;

use axum::http::HeaderValue;

/// The shortest token the runtime accepts. A token from Python's `secrets.token_urlsafe(32)` has 43
/// characters.
pub const MIN_LENGTH: usize = 32;

/// A control token that the runtime checks requests against.
#[derive(Clone, PartialEq, Eq)]
pub struct ControlToken(String);

/// A token file that the runtime cannot use.
#[derive(Debug)]
pub enum TokenError {
    /// The file cannot be read.
    Read(io::Error),
    /// The token is too short, or has characters other than letters, digits, `-` and `_`.
    Invalid,
}

impl fmt::Display for TokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(error) => write!(f, "the control token file cannot be read: {error}"),
            Self::Invalid => write!(
                f,
                "the control token must be at least {MIN_LENGTH} letters, digits, '-' or '_'"
            ),
        }
    }
}

impl std::error::Error for TokenError {}

impl fmt::Debug for ControlToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ControlToken(<redacted>)")
    }
}

impl ControlToken {
    /// Reads the token from `path`. Surrounding whitespace, such as a final newline, is ignored.
    pub fn read(path: &Path) -> Result<Self, TokenError> {
        let text = std::fs::read_to_string(path).map_err(TokenError::Read)?;
        Self::parse(text.trim())
    }

    /// Checks the form of a token and keeps it.
    pub fn parse(text: &str) -> Result<Self, TokenError> {
        let well_formed = text.len() >= MIN_LENGTH
            && text
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
        if well_formed {
            Ok(Self(text.to_owned()))
        } else {
            Err(TokenError::Invalid)
        }
    }

    /// Whether an `Authorization` header carries this token as a bearer credential.
    pub fn accepts(&self, authorization: Option<&HeaderValue>) -> bool {
        let expected = format!("Bearer {}", self.0);
        authorization.is_some_and(|value| constant_time_eq(value.as_bytes(), expected.as_bytes()))
    }
}

/// Compares two byte strings without stopping at the first difference, so that the time taken does
/// not reveal how much of the token matched. The length of the token is not secret.
fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "0123456789abcdefABCDEF0123456789-_xy";

    fn header(text: &str) -> HeaderValue {
        HeaderValue::from_str(text).expect("a valid header")
    }

    #[test]
    fn a_token_of_the_right_form_is_kept() {
        assert!(ControlToken::parse(TOKEN).is_ok());
    }

    #[test]
    fn a_short_or_strange_token_is_refused() {
        assert!(matches!(
            ControlToken::parse("short"),
            Err(TokenError::Invalid)
        ));
        let spaced = format!("{TOKEN} ");
        assert!(matches!(
            ControlToken::parse(&spaced),
            Err(TokenError::Invalid)
        ));
    }

    #[test]
    fn the_debug_form_does_not_show_the_token() {
        let token = ControlToken::parse(TOKEN).expect("valid");
        assert!(!format!("{token:?}").contains(TOKEN));
    }

    #[test]
    fn only_the_exact_bearer_credential_is_accepted() {
        let token = ControlToken::parse(TOKEN).expect("valid");
        assert!(token.accepts(Some(&header(&format!("Bearer {TOKEN}")))));
        assert!(!token.accepts(Some(&header(TOKEN))));
        assert!(!token.accepts(Some(&header(&format!("Bearer {TOKEN}x")))));
        assert!(!token.accepts(Some(&header("Bearer wrong"))));
        assert!(!token.accepts(None));
    }

    #[test]
    fn the_comparison_refuses_different_bytes_of_equal_length() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"ab"));
    }
}

//! The points of a station, read from a configuration file (task S2).
//!
//! The file is JSON:
//!
//! ```json
//! {"schema_version": 1, "points": [{"address": 1, "kind": "single", "value": true, "stamped": false}]}
//! ```
//!
//! Each point has one kind, and its value is written in the form the kind table below gives.
//! A time-tagged point (`stamped`) reports its changes with their time. The address is an
//! information object address, 1 to 16777215, and each address is used once (IMPLEMENTATION.md §5).

use std::fmt;
use std::path::Path;

use igs_iec104::process_image::PointValue;
use igs_iec104_codec::elements::{Diq, DoublePoint, Nva, Qds, QualityFlags, ShortFloat, Siq, Sva};
use igs_iec104_codec::header::InformationObjectAddress;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The version of the points file this runtime reads.
pub const SCHEMA_VERSION: u32 = 1;

/// The largest information object address (three octets).
const MAX_ADDRESS: u32 = 16_777_215;

/// The kind of a point: the monitoring type it is served as.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// Single-point information: M_SP_NA_1, or M_SP_TB_1 when stamped. Its value is a boolean.
    Single,
    /// Double-point information: M_DP_NA_1, or M_DP_TB_1 when stamped. Its value is one of
    /// `off`, `on`, `intermediate`, or `indeterminate`.
    Double,
    /// Normalized value: M_ME_NA_1, or M_ME_TD_1 when stamped. Its value is a number from -1 up to
    /// 1, exclusive of 1, on a grid of 2^-15.
    Normalized,
    /// Scaled value: M_ME_NB_1, or M_ME_TE_1 when stamped. Its value is an integer from -32768 to
    /// 32767.
    Scaled,
    /// Short floating point: M_ME_NC_1, or M_ME_TF_1 when stamped. Its value is a finite number.
    Float,
}

impl Kind {
    /// The value the kind takes, for a message.
    fn expected(self) -> &'static str {
        match self {
            Self::Single => "true or false",
            Self::Double => "\"off\", \"on\", \"intermediate\" or \"indeterminate\"",
            Self::Normalized => "a number from -1 up to 1, excluding 1",
            Self::Scaled => "an integer from -32768 to 32767",
            Self::Float => "a finite number",
        }
    }
}

/// One point of the station, as written in the configuration.
#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PointSpec {
    /// The information object address, 1 to 16777215.
    pub address: u32,
    /// The kind of the point.
    pub kind: Kind,
    /// The value, in the form of the kind.
    pub value: Value,
    /// Whether the point is time-tagged.
    #[serde(default)]
    pub stamped: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PointFile {
    schema_version: u32,
    points: Vec<PointSpec>,
}

/// A points file that the runtime cannot use.
#[derive(Debug)]
pub enum PointError {
    /// The file cannot be read.
    Read {
        /// The path of the file.
        path: String,
        /// The reason.
        error: std::io::Error,
    },
    /// The file is not a valid points file: a syntax error, or a field that is not known.
    Parse(String),
    /// The file has a schema version this runtime does not read.
    SchemaVersion(u32),
    /// An address outside 1 to 16777215.
    Address(u32),
    /// An address used by two points.
    Duplicate(u32),
    /// A value that does not fit the kind of its point.
    Value {
        /// The address of the point.
        address: u32,
        /// The kind of the point.
        kind: Kind,
        /// The form the value must take.
        expected: &'static str,
    },
}

impl fmt::Display for PointError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read { path, error } => write!(f, "cannot read the points file {path}: {error}"),
            Self::Parse(reason) => write!(f, "the points file is not valid: {reason}"),
            Self::SchemaVersion(found) => write!(
                f,
                "the points file has schema_version {found}, this runtime reads {SCHEMA_VERSION}"
            ),
            Self::Address(address) => {
                write!(
                    f,
                    "address {address} is out of range: use 1 to {MAX_ADDRESS}"
                )
            }
            Self::Duplicate(address) => write!(f, "address {address} is used by two points"),
            Self::Value {
                address,
                kind,
                expected,
            } => write!(
                f,
                "point {address} is {kind:?}, so its value must be {expected}"
            ),
        }
    }
}

impl std::error::Error for PointError {}

/// Reads and checks the points of the file at `path`.
pub fn load(path: &Path) -> Result<Vec<PointSpec>, PointError> {
    let text = std::fs::read_to_string(path).map_err(|error| PointError::Read {
        path: path.display().to_string(),
        error,
    })?;
    parse(&text)
}

/// Reads and checks the points of a file's text: the schema version, the addresses, the
/// duplicates, and the value of each point.
pub fn parse(text: &str) -> Result<Vec<PointSpec>, PointError> {
    let file: PointFile =
        serde_json::from_str(text).map_err(|error| PointError::Parse(error.to_string()))?;
    if file.schema_version != SCHEMA_VERSION {
        return Err(PointError::SchemaVersion(file.schema_version));
    }
    let mut addresses = std::collections::BTreeSet::new();
    for point in &file.points {
        address(point.address)?;
        if !addresses.insert(point.address) {
            return Err(PointError::Duplicate(point.address));
        }
        point.point_value()?;
    }
    Ok(file.points)
}

/// The information object address for `address`, when it is in range.
pub fn address(address: u32) -> Result<InformationObjectAddress, PointError> {
    if address == 0 {
        return Err(PointError::Address(address));
    }
    InformationObjectAddress::new(address).ok_or(PointError::Address(address))
}

impl PointSpec {
    /// The value of the point, as the library stores it.
    pub fn point_value(&self) -> Result<PointValue, PointError> {
        value_of(self.kind, self.address, &self.value)
    }
}

/// The value of a point of `kind` at `address`, from its JSON form.
pub fn value_of(kind: Kind, address: u32, json: &Value) -> Result<PointValue, PointError> {
    let invalid = || PointError::Value {
        address,
        kind,
        expected: kind.expected(),
    };
    let quality = Qds::default();
    Ok(match kind {
        Kind::Single => PointValue::Single(Siq {
            on: json.as_bool().ok_or_else(invalid)?,
            quality: QualityFlags::default(),
        }),
        Kind::Double => PointValue::Double(Diq {
            state: double_from(json).ok_or_else(invalid)?,
            quality: QualityFlags::default(),
        }),
        Kind::Normalized => {
            let number = json.as_f64().ok_or_else(invalid)?;
            let value = Nva::try_from_f32(number as f32).ok_or_else(invalid)?;
            PointValue::Normalized(value, quality)
        }
        Kind::Scaled => {
            let number = json.as_i64().ok_or_else(invalid)?;
            let raw = i16::try_from(number).map_err(|_| invalid())?;
            PointValue::Scaled(Sva::new(raw), quality)
        }
        Kind::Float => {
            let number = json.as_f64().filter(|number| number.is_finite());
            let number = number.ok_or_else(invalid)?;
            PointValue::Float(ShortFloat::from_f32(number as f32), quality)
        }
    })
}

fn double_from(json: &Value) -> Option<DoublePoint> {
    match json.as_str()? {
        "off" => Some(DoublePoint::Off),
        "on" => Some(DoublePoint::On),
        "intermediate" => Some(DoublePoint::IndeterminateOrIntermediate),
        "indeterminate" => Some(DoublePoint::Indeterminate),
        _ => None,
    }
}

/// The JSON form of a value, as the control API reports it.
pub fn json_of(value: PointValue) -> Value {
    match value {
        PointValue::Single(siq) => Value::Bool(siq.on),
        PointValue::Double(diq) => Value::String(
            match diq.state {
                DoublePoint::Off => "off",
                DoublePoint::On => "on",
                DoublePoint::IndeterminateOrIntermediate => "intermediate",
                DoublePoint::Indeterminate => "indeterminate",
            }
            .to_owned(),
        ),
        PointValue::Normalized(nva, _) => number(f64::from(nva.as_f32())),
        PointValue::Scaled(sva, _) => Value::from(sva.value()),
        PointValue::Float(float, _) => number(f64::from(float.as_f32())),
        _ => Value::Null,
    }
}

fn number(value: f64) -> Value {
    serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(points: &str) -> String {
        format!("{{\"schema_version\": 1, \"points\": [{points}]}}")
    }

    #[test]
    fn a_valid_file_gives_its_points() {
        let text = file(
            r#"{"address": 1, "kind": "single", "value": true},
               {"address": 2, "kind": "float", "value": 1.5, "stamped": true}"#,
        );
        let points = parse(&text).expect("valid");
        assert_eq!(points.len(), 2);
        assert!(!points[0].stamped);
        assert!(points[1].stamped);
    }

    #[test]
    fn an_unknown_field_is_refused() {
        let text = file(r#"{"address": 1, "kind": "single", "value": true, "group": 1}"#);
        assert!(matches!(parse(&text), Err(PointError::Parse(_))));
    }

    #[test]
    fn the_schema_version_must_be_the_one_read() {
        let text = r#"{"schema_version": 2, "points": []}"#;
        assert!(matches!(parse(text), Err(PointError::SchemaVersion(2))));
    }

    #[test]
    fn the_addresses_are_checked() {
        for address in [0, MAX_ADDRESS + 1] {
            let text = file(&format!(
                r#"{{"address": {address}, "kind": "single", "value": true}}"#
            ));
            assert!(matches!(parse(&text), Err(PointError::Address(_))));
        }
        let top = file(&format!(
            r#"{{"address": {MAX_ADDRESS}, "kind": "single", "value": true}}"#
        ));
        assert!(parse(&top).is_ok());
    }

    #[test]
    fn an_address_used_twice_is_refused() {
        let text = file(
            r#"{"address": 7, "kind": "single", "value": true},
               {"address": 7, "kind": "single", "value": false}"#,
        );
        assert!(matches!(parse(&text), Err(PointError::Duplicate(7))));
    }

    #[test]
    fn a_value_of_the_wrong_form_is_refused_with_its_point() {
        let cases = [
            ("single", "1"),
            ("double", "\"maybe\""),
            ("normalized", "1.0"),
            ("scaled", "40000"),
            ("float", "\"x\""),
        ];
        for (kind, value) in cases {
            let text = file(&format!(
                r#"{{"address": 3, "kind": "{kind}", "value": {value}}}"#
            ));
            assert!(
                matches!(parse(&text), Err(PointError::Value { address: 3, .. })),
                "{kind} with {value} must be refused"
            );
        }
    }

    #[test]
    fn every_kind_reads_back_as_the_json_it_was_given() {
        let cases = [
            (Kind::Single, Value::Bool(true)),
            (Kind::Double, Value::String("intermediate".to_owned())),
            (Kind::Normalized, Value::from(0.5)),
            (Kind::Scaled, Value::from(-700)),
            (Kind::Float, Value::from(12.5)),
        ];
        for (kind, json) in cases {
            let value = value_of(kind, 1, &json).expect("valid");
            assert_eq!(json_of(value), json, "{kind:?}");
        }
    }
}

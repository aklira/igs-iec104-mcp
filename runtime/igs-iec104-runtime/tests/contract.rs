//! Contract tests of the runtime (task C1): the events of the client match
//! `contracts/events.schema.json`, for every kind of event.
//!
//! The check is a reader of the subset of JSON Schema the contract uses: object fields, required
//! fields, `const`, `enum`, `type`, the integer ranges, `items`, and `$ref` into `$defs`. The
//! Python contract tests validate the same events with a full JSON Schema validator.

use std::path::PathBuf;

use igs_iec104_runtime::events::{AsduView, EventKind, EventLog};
use igs_iec104_runtime::image::Quality;
use igs_iec104_runtime::station_log::{StationEventKind, StationLog};
use serde_json::{json, Value};

fn schema() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts/events.schema.json");
    let text = std::fs::read_to_string(path).expect("the events schema is read");
    serde_json::from_str(&text).expect("the events schema is JSON")
}

fn view() -> AsduView {
    AsduView {
        type_id: 1,
        cause: 20,
        negative: false,
        common_address: 1,
        bytes_hex: Some("01011400010001000001".to_owned()),
    }
}

/// One event of each kind the client records.
fn samples() -> Vec<EventKind> {
    vec![
        EventKind::Connected,
        EventKind::Switched { from: None, to: 0 },
        EventKind::Switched {
            from: Some(0),
            to: 1,
        },
        EventKind::Disconnected {
            error: "the peer closed the connection".to_owned(),
        },
        EventKind::ConnectFailed {
            error: "connection refused".to_owned(),
        },
        EventKind::Discarded {
            command: "StartDt".to_owned(),
        },
        EventKind::Transfer { state: "started" },
        EventKind::Asdu { asdu: view() },
        EventKind::Point {
            address: 100,
            type_id: 13,
            value: json!(12.5),
            cause: 3,
            common_address: 1,
            quality: Quality {
                invalid: false,
                not_topical: false,
                substituted: false,
                blocked: false,
                overflow: false,
            },
            time: Some("2026-10-11T12:00:00.250".to_owned()),
        },
        EventKind::Transmitted { asdu: view() },
        EventKind::Rejected {
            detail: "TransferStopped".to_owned(),
        },
        EventKind::Unacknowledged {
            asdus: vec![view()],
        },
        EventKind::Closed,
        EventKind::Verdict {
            request: 7,
            outcome: "refused",
            sent: true,
            cause: Some(47),
            negative: Some(false),
            transmitted_generation: Some(2),
        },
    ]
}

/// The name of the definition of a kind of event in the schema.
fn definition_of(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "connected" => "Connected",
        "switched" => "Switched",
        "disconnected" => "Disconnected",
        "connect_failed" => "ConnectFailed",
        "discarded" => "Discarded",
        "transfer" => "Transfer",
        "asdu" => "Asdu",
        "point" => "Point",
        "transmitted" => "Transmitted",
        "rejected" => "Rejected",
        "unacknowledged" => "Unacknowledged",
        "closed" => "Closed",
        "verdict" => "Verdict",
        _ => return None,
    })
}

/// Checks a value against a schema fragment, resolving `$ref` into the schema's `$defs`.
fn check(schema: &Value, fragment: &Value, value: &Value) -> Result<(), String> {
    if let Some(pointer) = fragment.get("$ref").and_then(Value::as_str) {
        // A reference to a definition, in this file or in another one: the part after `#/$defs/`.
        let (_, name) = pointer
            .rsplit_once("#/$defs/")
            .ok_or_else(|| format!("unsupported reference {pointer}"))?;
        return check(schema, &schema["$defs"][name], value);
    }
    if let Some(expected) = fragment.get("const") {
        if value != expected {
            return Err(format!("{value} is not {expected}"));
        }
    }
    if let Some(choices) = fragment.get("enum").and_then(Value::as_array) {
        if !choices.contains(value) {
            return Err(format!("{value} is not one of {choices:?}"));
        }
    }
    match fragment.get("type") {
        Some(Value::String(kind)) => check_type(schema, fragment, kind, value),
        Some(Value::Array(kinds)) => {
            let fits = kinds
                .iter()
                .filter_map(Value::as_str)
                .any(|kind| check_type(schema, fragment, kind, value).is_ok());
            if fits {
                Ok(())
            } else {
                Err(format!("{value} is none of the types {kinds:?}"))
            }
        }
        _ => Ok(()),
    }
}

fn check_type(schema: &Value, fragment: &Value, kind: &str, value: &Value) -> Result<(), String> {
    match kind {
        "object" => {
            let object = value
                .as_object()
                .ok_or_else(|| format!("{value} is not an object"))?;
            let properties = fragment["properties"]
                .as_object()
                .ok_or_else(|| "an object without properties".to_owned())?;
            for field in fragment["required"].as_array().into_iter().flatten() {
                let name = field.as_str().unwrap_or_default();
                if !object.contains_key(name) {
                    return Err(format!("{name} is missing"));
                }
            }
            for name in object.keys() {
                if !properties.contains_key(name) {
                    return Err(format!("{name} is not a field of this object"));
                }
            }
            for (name, spec) in properties {
                if let Some(field) = object.get(name) {
                    check(schema, spec, field).map_err(|error| format!("{name}: {error}"))?;
                }
            }
            Ok(())
        }
        "array" => {
            let items = value
                .as_array()
                .ok_or_else(|| format!("{value} is not an array"))?;
            items
                .iter()
                .try_for_each(|item| check(schema, &fragment["items"], item))
        }
        "string" => value
            .is_string()
            .then_some(())
            .ok_or_else(|| format!("{value} is not a string")),
        "boolean" => value
            .is_boolean()
            .then_some(())
            .ok_or_else(|| format!("{value} is not a boolean")),
        "null" => value
            .is_null()
            .then_some(())
            .ok_or_else(|| format!("{value} is not null")),
        "integer" => {
            let number = value
                .as_i64()
                .or_else(|| value.as_u64().and_then(|n| i64::try_from(n).ok()))
                .ok_or_else(|| format!("{value} is not an integer"))?;
            if let Some(minimum) = fragment.get("minimum").and_then(Value::as_i64) {
                if number < minimum {
                    return Err(format!("{number} is below {minimum}"));
                }
            }
            if let Some(maximum) = fragment.get("maximum").and_then(Value::as_i64) {
                if number > maximum {
                    return Err(format!("{number} is above {maximum}"));
                }
            }
            Ok(())
        }
        other => Err(format!("the checker does not read the type {other}")),
    }
}

#[test]
fn every_kind_the_client_records_matches_the_events_schema() {
    let schema = schema();
    for kind in samples() {
        let mut log = EventLog::new(4);
        log.record_at(kind.clone(), 1_700_000_000_000, 3);
        let recorded = log.after(0).pop().expect("the event is kept");
        let event = serde_json::to_value(&recorded).expect("the event is JSON");
        let name = event["kind"].as_str().expect("the event has a kind");
        let definition = definition_of(name).expect("the kind is in the contract");
        let fragment = &schema["$defs"][definition];
        if let Err(error) = check(&schema, fragment, &event) {
            panic!("{name} does not match its definition: {error}: {event}");
        }
    }
}

#[test]
fn the_schema_describes_every_kind_the_client_can_record() {
    let schema = schema();
    let mut declared: Vec<&str> = schema["$defs"]["Event"]["oneOf"]
        .as_array()
        .expect("the event is a choice of definitions")
        .iter()
        .map(|choice| {
            let pointer = choice["$ref"].as_str().unwrap_or_default();
            pointer.trim_start_matches("#/$defs/")
        })
        .collect();
    declared.sort_unstable();
    let mut sampled: Vec<&str> = samples()
        .iter()
        .map(|kind| {
            let mut log = EventLog::new(1);
            log.record_at(kind.clone(), 0, 1);
            let event = serde_json::to_value(log.after(0).pop().expect("kept")).expect("JSON");
            definition_of(event["kind"].as_str().unwrap_or_default()).unwrap_or_default()
        })
        .collect();
    sampled.sort_unstable();
    // Two samples of a switchover (with and without a first station) name one kind.
    sampled.dedup();
    assert_eq!(
        declared, sampled,
        "the schema and the runtime name the same kinds"
    );
}

#[test]
fn the_checker_refuses_an_event_outside_the_contract() {
    let schema = schema();
    let fragment = &schema["$defs"]["Asdu"];
    let outside = [
        // A type identification beyond one octet.
        json!({"seq": 1, "observed_at_ms": 0, "generation": 1, "kind": "asdu", "asdu": {"type_id": 300, "cause": 20, "negative": false, "common_address": 1, "bytes_hex": null}}),
        // A field the contract does not have.
        json!({"seq": 1, "observed_at_ms": 0, "generation": 1, "kind": "asdu", "asdu": {"type_id": 1, "cause": 20, "negative": false, "common_address": 1, "bytes_hex": null}, "extra": true}),
        // A sequence number of zero: the first event is number 1.
        json!({"seq": 0, "observed_at_ms": 0, "generation": 1, "kind": "asdu", "asdu": {"type_id": 1, "cause": 20, "negative": false, "common_address": 1, "bytes_hex": null}}),
    ];
    for event in outside {
        assert!(
            check(&schema, fragment, &event).is_err(),
            "the checker must refuse {event}"
        );
    }
}

/// The schema of the station's events, with the transfer states of the client's schema, which the
/// station's schema refers to.
fn station_schema() -> Value {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts");
    let text = std::fs::read_to_string(dir.join("station-events.schema.json"))
        .expect("the station events schema is read");
    let mut schema: Value = serde_json::from_str(&text).expect("the station events schema is JSON");
    schema["$defs"]["TransferState"] = schema_of_events()["$defs"]["TransferState"].clone();
    schema
}

fn schema_of_events() -> Value {
    schema()
}

/// The definition of a kind of station event in the schema.
fn station_definition_of(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "connected" => "Connected",
        "transfer" => "Transfer",
        "superseded" => "Superseded",
        "closed" => "Closed",
        "command" => "Command",
        "security" => "Security",
        _ => return None,
    })
}

/// One event of each kind the station records.
fn station_samples() -> Vec<StationEventKind> {
    vec![
        StationEventKind::Connected {
            connection: 1,
            peer: "127.0.0.1:50000".to_owned(),
        },
        StationEventKind::Transfer {
            connection: 1,
            state: "started",
        },
        StationEventKind::Superseded {
            connection: 1,
            by: 2,
        },
        StationEventKind::Closed {
            connection: 1,
            end: "transport",
            error: Some("the peer closed the stream".to_owned()),
        },
        StationEventKind::Command {
            address: 1,
            command: "single",
            select: true,
            accepted: false,
        },
        StationEventKind::Security {
            mnemonic: "NoCaMatch".to_owned(),
            severity: "alarm".to_owned(),
            identifier: "IEC 62351-3:6.4".to_owned(),
            detail: "the chain has no trust anchor".to_owned(),
        },
    ]
}

#[test]
fn every_station_event_matches_the_station_schema() {
    let schema = station_schema();
    for kind in station_samples() {
        let mut log = StationLog::new(4);
        log.record(kind);
        let recorded = log.after(0).pop().expect("the event is kept");
        let event = serde_json::to_value(&recorded).expect("the event is JSON");
        let name = event["kind"].as_str().expect("the event has a kind");
        let definition = station_definition_of(name).expect("the kind is in the contract");
        if let Err(error) = check(&schema, &schema["$defs"][definition], &event) {
            panic!("{name} does not match its definition: {error}: {event}");
        }
    }
}

#[test]
fn the_station_schema_describes_every_kind_the_station_records() {
    let schema = station_schema();
    let mut declared: Vec<&str> = schema["$defs"]["StationEvent"]["oneOf"]
        .as_array()
        .expect("the event is a choice of definitions")
        .iter()
        .map(|choice| {
            choice["$ref"]
                .as_str()
                .unwrap_or_default()
                .rsplit('/')
                .next()
                .unwrap_or_default()
        })
        .collect();
    declared.sort_unstable();
    let mut sampled: Vec<&str> = station_samples()
        .into_iter()
        .map(|kind| {
            let mut log = StationLog::new(1);
            log.record(kind);
            let event = serde_json::to_value(log.after(0).pop().expect("kept")).expect("JSON");
            station_definition_of(event["kind"].as_str().unwrap_or_default()).unwrap_or_default()
        })
        .collect();
    sampled.sort_unstable();
    assert_eq!(declared, sampled);
}

#[test]
fn an_event_of_a_group_station_names_the_station_and_matches_the_schema() {
    let schema = schema();
    let mut log = EventLog::new(4);
    log.record_from(EventKind::Connected, Some(1), 2);
    let recorded = log.after(0).pop().expect("the event is kept");
    let event = serde_json::to_value(&recorded).expect("the event is JSON");
    assert_eq!(event["endpoint"], 1);
    let fragment = &schema["$defs"]["Connected"];
    if let Err(error) = check(&schema, fragment, &event) {
        panic!("the station's event does not match its definition: {error}: {event}");
    }
}

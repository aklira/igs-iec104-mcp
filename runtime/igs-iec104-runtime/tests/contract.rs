//! Contract tests of the runtime (task C1): the events of the client match
//! `contracts/events.schema.json`, for every kind of event.
//!
//! The check is a reader of the subset of JSON Schema the contract uses: object fields, required
//! fields, `const`, `enum`, `type`, the integer ranges, `items`, and `$ref` into `$defs`. The
//! Python contract tests validate the same events with a full JSON Schema validator.

use std::path::PathBuf;

use igs_iec104_runtime::events::{AsduView, EventKind, EventLog};
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
        "disconnected" => "Disconnected",
        "connect_failed" => "ConnectFailed",
        "discarded" => "Discarded",
        "transfer" => "Transfer",
        "asdu" => "Asdu",
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
        let name = pointer
            .strip_prefix("#/$defs/")
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

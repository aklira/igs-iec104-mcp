//! The events of the client, kept for the control API in arrival order.
//!
//! Each event has a sequence number. A reader that asks for the events after a number can see
//! whether it missed any: the oldest kept sequence number is reported with every answer.

use std::collections::VecDeque;
use std::time::{SystemTime, UNIX_EPOCH};

use igs_iec104_codec::asdu::Asdu;
use serde::Serialize;
use serde_json::Value;

use crate::image::{self, Quality};
use crate::points;

/// An event as the control API reports it. The variants are part of the API until C1 fixes its
/// schema.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EventKind {
    /// The connection to the station is established.
    Connected,
    /// The started station of a redundancy group changed: `from` was started before (none at the
    /// first start), `to` is started now (IEC 60870-5-104 §10.5).
    Switched {
        /// The station started before, or none at the first start.
        from: Option<usize>,
        /// The station started now.
        to: usize,
    },
    /// The connection ended, with this error. The client reconnects.
    Disconnected {
        /// The error, as text.
        error: String,
    },
    /// A connection attempt failed.
    ConnectFailed {
        /// The error, as text.
        error: String,
    },
    /// A request was dropped because there was no connection. It was not sent.
    Discarded {
        /// The request, as text.
        command: String,
    },
    /// The state of the data transfer changed.
    Transfer {
        /// `stopped`, `pending_started`, `started`, `pending_stopped`, or
        /// `pending_unconfirmed_stop`.
        state: &'static str,
    },
    /// An ASDU of the station.
    Asdu {
        /// The ASDU.
        asdu: AsduView,
    },
    /// A point value that an ASDU of the station carried, decoded: one event per point, recorded
    /// right after the ASDU that carried it (task E2b).
    Point {
        /// The information object address of the point.
        address: u32,
        /// The type identification of the ASDU.
        type_id: u8,
        /// The value, in the form of its kind.
        value: Value,
        /// The cause of transmission of the ASDU, without the P/N and test bits.
        cause: u8,
        /// The common address of the ASDU.
        common_address: u16,
        /// The quality of the value.
        quality: Quality,
        /// The time tag, for a time-tagged type.
        time: Option<String>,
    },
    /// An ASDU of this client was written to the connection.
    Transmitted {
        /// The ASDU.
        asdu: AsduView,
    },
    /// A request was not carried out, with the reason as text.
    Rejected {
        /// The rejection, as text.
        detail: String,
    },
    /// The ASDUs that the station did not acknowledge when the connection ended, oldest first.
    Unacknowledged {
        /// The ASDUs.
        asdus: Vec<AsduView>,
    },
    /// The client stopped.
    Closed,
    /// A request ended. `sent` tells whether its I frame was written, and `transmitted_generation`
    /// the connection it was written on.
    Verdict {
        /// The request.
        request: u64,
        /// `confirmed`, `refused`, `timed_out`, `cancelled`, `indeterminate`, or `not_sent`.
        outcome: &'static str,
        /// Whether the I frame was written.
        sent: bool,
        /// For a refusal: the cause of the station's answer.
        cause: Option<u8>,
        /// For a refusal: true for a negative confirmation.
        negative: Option<bool>,
        /// The connection generation the request was written on, if it was.
        transmitted_generation: Option<u64>,
    },
}

/// An ASDU as the control API reports it: its header, and its encoding in hexadecimal. The
/// decoded form is defined with the contract (task C1).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AsduView {
    /// The type identification.
    pub type_id: u8,
    /// The cause of transmission.
    pub cause: u8,
    /// The negative confirmation bit (P/N).
    pub negative: bool,
    /// The common address.
    pub common_address: u16,
    /// The encoding of the ASDU, in lower-case hexadecimal, or `null` when it cannot be encoded.
    pub bytes_hex: Option<String>,
}

impl AsduView {
    /// The view of an ASDU.
    pub fn of(asdu: &Asdu) -> Self {
        Self {
            type_id: asdu.type_id() as u8,
            cause: asdu.cot.cause(),
            negative: asdu.cot.negative,
            common_address: asdu.common_address.value(),
            bytes_hex: asdu.to_vec().ok().map(|bytes| hex(&bytes)),
        }
    }
}

/// The point events of an ASDU: one per value it carries, in the order of the ASDU.
pub fn points_of(asdu: &Asdu) -> Vec<EventKind> {
    image::observations(asdu)
        .into_iter()
        .map(|(address, observed)| EventKind::Point {
            address,
            type_id: asdu.type_id() as u8,
            value: points::json_of(observed.value),
            cause: asdu.cot.cause(),
            common_address: asdu.common_address.value(),
            quality: observed.quality,
            time: observed.time.map(image::time_text),
        })
        .collect()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// An event with its sequence number and the time it was recorded.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Recorded {
    /// The sequence number, from 1, with no gap while the events are kept.
    pub seq: u64,
    /// The time the event was recorded, in milliseconds since the Unix epoch (UTC).
    pub observed_at_ms: u64,
    /// The connection generation the event was recorded in: the number of connections so far.
    pub generation: u64,
    /// The station of a redundancy group the event came from, in the order the stations were
    /// given. None for a client of one station, and for an event of the group itself.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<usize>,
    /// The event.
    #[serde(flatten)]
    pub kind: EventKind,
}

/// The events of the client that are kept, with the oldest dropped first when full.
#[derive(Debug)]
pub struct EventLog {
    capacity: usize,
    next_seq: u64,
    events: VecDeque<Recorded>,
    dropped: u64,
}

impl EventLog {
    /// An empty log that keeps `capacity` events. A capacity of zero keeps nothing.
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            next_seq: 1,
            events: VecDeque::with_capacity(capacity.min(1024)),
            dropped: 0,
        }
    }

    /// Records an event at the current time, in `generation`, and returns its sequence number.
    pub fn record(&mut self, kind: EventKind, generation: u64) -> u64 {
        self.record_from(kind, None, generation)
    }

    /// Records an event of the station `endpoint` of a redundancy group (or of the client, when
    /// there is none), at the current time, in `generation`.
    pub fn record_from(
        &mut self,
        kind: EventKind,
        endpoint: Option<usize>,
        generation: u64,
    ) -> u64 {
        self.record_observed(kind, now_ms(), endpoint, generation)
    }

    /// Records an event observed at `observed_at_ms`, in `generation`, and returns its sequence
    /// number.
    pub fn record_at(&mut self, kind: EventKind, observed_at_ms: u64, generation: u64) -> u64 {
        self.record_observed(kind, observed_at_ms, None, generation)
    }

    fn record_observed(
        &mut self,
        kind: EventKind,
        observed_at_ms: u64,
        endpoint: Option<usize>,
        generation: u64,
    ) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        if self.capacity == 0 {
            self.dropped += 1;
            return seq;
        }
        if self.events.len() == self.capacity {
            self.events.pop_front();
            self.dropped += 1;
        }
        self.events.push_back(Recorded {
            seq,
            observed_at_ms,
            generation,
            endpoint,
            kind,
        });
        seq
    }

    /// The events recorded after `seq`, oldest first.
    pub fn after(&self, seq: u64) -> Vec<Recorded> {
        self.events
            .iter()
            .filter(|event| event.seq > seq)
            .cloned()
            .collect()
    }

    /// The sequence number of the oldest event kept, or `None` when none is kept.
    pub fn first_seq(&self) -> Option<u64> {
        self.events.front().map(|event| event.seq)
    }

    /// The sequence number of the last event recorded, or 0 when none was.
    pub fn last_seq(&self) -> u64 {
        self.next_seq - 1
    }

    /// The number of events dropped because the log was full.
    pub fn dropped(&self) -> u64 {
        self.dropped
    }
}

/// The current time in milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connected() -> EventKind {
        EventKind::Connected
    }

    #[test]
    fn sequence_numbers_start_at_one_and_grow() {
        let mut log = EventLog::new(8);
        assert_eq!(log.record_at(connected(), 1, 1), 1);
        assert_eq!(log.record_at(connected(), 2, 1), 2);
        assert_eq!(log.last_seq(), 2);
        assert_eq!(log.first_seq(), Some(1));
    }

    #[test]
    fn the_oldest_events_are_dropped_and_counted_when_full() {
        let mut log = EventLog::new(2);
        for at in 1..=5 {
            log.record_at(connected(), at, 1);
        }
        assert_eq!(log.dropped(), 3);
        assert_eq!(log.first_seq(), Some(4));
        let seqs: Vec<u64> = log.after(0).iter().map(|event| event.seq).collect();
        assert_eq!(seqs, vec![4, 5]);
    }

    #[test]
    fn a_reader_sees_only_the_events_after_its_number() {
        let mut log = EventLog::new(8);
        for at in 1..=3 {
            log.record_at(connected(), at, 1);
        }
        let seqs: Vec<u64> = log.after(2).iter().map(|event| event.seq).collect();
        assert_eq!(seqs, vec![3]);
        assert!(log.after(3).is_empty());
    }

    #[test]
    fn a_zero_capacity_log_keeps_nothing_and_counts_every_event() {
        let mut log = EventLog::new(0);
        log.record_at(connected(), 1, 1);
        log.record_at(connected(), 2, 1);
        assert!(log.after(0).is_empty());
        assert_eq!(log.first_seq(), None);
        assert_eq!(log.dropped(), 2);
        assert_eq!(log.last_seq(), 2);
    }

    #[test]
    fn hexadecimal_is_lower_case_and_two_digits_per_byte() {
        assert_eq!(hex(&[0x00, 0x0a, 0xff]), "000aff");
    }
}

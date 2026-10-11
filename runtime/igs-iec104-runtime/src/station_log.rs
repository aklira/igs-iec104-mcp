//! The events of a station, kept for the control API (task C3): its connections, its transfers, and
//! the commands it judged. Each event has a sequence number, so that a reader can see what it missed.

use std::collections::VecDeque;

use serde::Serialize;

use crate::events::now_ms;

/// What happened at the station.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum StationEventKind {
    /// A connection was accepted.
    Connected {
        /// The identity of the connection.
        connection: u64,
        /// The address of the controlling station.
        peer: String,
    },
    /// The data transfer of a connection changed state.
    Transfer {
        /// The identity of the connection.
        connection: u64,
        /// The new state, named as the client names it.
        state: &'static str,
    },
    /// Another connection started its transfer, so this one was closed.
    Superseded {
        /// The identity of the connection that was closed.
        connection: u64,
        /// The identity of the connection that started.
        by: u64,
    },
    /// A connection ended.
    Closed {
        /// The identity of the connection.
        connection: u64,
        /// `station`, `transport`, or `forced`.
        end: &'static str,
        /// The error that ended it, for a transport end.
        error: Option<String>,
    },
    /// A command was judged.
    Command {
        /// The address of the command.
        address: u32,
        /// `single`, `double`, `step`, `normalized`, `scaled`, or `float`.
        command: &'static str,
        /// True for a select, false for an execution.
        select: bool,
        /// Whether the station confirmed the command.
        accepted: bool,
    },
}

/// An event of the station with its sequence number and the time it was recorded.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StationRecorded {
    /// The sequence number, from 1.
    pub seq: u64,
    /// The time the event was recorded, in milliseconds since the Unix epoch (UTC).
    pub observed_at_ms: u64,
    /// The event.
    #[serde(flatten)]
    pub kind: StationEventKind,
}

/// The events of the station that are kept, the oldest dropped first when full.
#[derive(Debug)]
pub struct StationLog {
    capacity: usize,
    next_seq: u64,
    events: VecDeque<StationRecorded>,
    /// Events lost before they reached the log: dropped by the station's stream, or by the command
    /// queue. Set by the runtime from the counters of those queues.
    lost: u64,
    /// Events the log dropped to keep its capacity.
    evicted: u64,
}

impl StationLog {
    /// An empty log that keeps `capacity` events.
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            next_seq: 1,
            events: VecDeque::with_capacity(capacity.min(1024)),
            lost: 0,
            evicted: 0,
        }
    }

    /// Records an event at the current time and returns its sequence number.
    pub fn record(&mut self, kind: StationEventKind) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        if self.capacity == 0 {
            self.evicted += 1;
            return seq;
        }
        if self.events.len() == self.capacity {
            self.events.pop_front();
            self.evicted += 1;
        }
        self.events.push_back(StationRecorded {
            seq,
            observed_at_ms: now_ms(),
            kind,
        });
        seq
    }

    /// Sets the number of events lost before the log, as the queues count them.
    pub fn set_lost(&mut self, lost: u64) {
        self.lost = lost;
    }

    /// The events after `seq`, oldest first.
    pub fn after(&self, seq: u64) -> Vec<StationRecorded> {
        self.events
            .iter()
            .filter(|event| event.seq > seq)
            .cloned()
            .collect()
    }

    /// The oldest sequence number kept, or `None` when none is kept.
    pub fn first_seq(&self) -> Option<u64> {
        self.events.front().map(|event| event.seq)
    }

    /// The last sequence number recorded, or 0 when none was.
    pub fn last_seq(&self) -> u64 {
        self.next_seq - 1
    }

    /// The events the station lost: those the queues dropped, and those the log evicted.
    pub fn dropped(&self) -> u64 {
        self.lost + self.evicted
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command() -> StationEventKind {
        StationEventKind::Command {
            address: 1,
            command: "single",
            select: true,
            accepted: true,
        }
    }

    #[test]
    fn sequence_numbers_start_at_one() {
        let mut log = StationLog::new(4);
        assert_eq!(log.record(command()), 1);
        assert_eq!(log.record(command()), 2);
        assert_eq!((log.first_seq(), log.last_seq()), (Some(1), 2));
    }

    #[test]
    fn events_the_queues_lost_and_the_log_evicted_are_counted() {
        let mut log = StationLog::new(2);
        for _ in 0..4 {
            log.record(command());
        }
        log.set_lost(3);
        assert_eq!(log.dropped(), 3 + 2);
        assert_eq!(log.first_seq(), Some(3));
    }

    #[test]
    fn a_reader_gets_the_events_after_its_number() {
        let mut log = StationLog::new(8);
        for _ in 0..3 {
            log.record(command());
        }
        let seqs: Vec<u64> = log.after(1).iter().map(|event| event.seq).collect();
        assert_eq!(seqs, vec![2, 3]);
    }
}

//! The requests of the client and their verdicts (task C2, IMPLEMENTATION.md §7).
//!
//! A request is an ASDU the client sends: an interrogation, or a single command. Its verdict is
//! decided from the events of the connection, which come in order, and nothing else: no I/O here,
//! so every rule is tested with an injected clock.
//!
//! The rules:
//!
//! - A request is queued when it is accepted, then transmitted when its I frame is written. A
//!   request the client discards without a connection was not sent: `not_sent`.
//! - The connection generation counts the connections. An answer is judged only against a request
//!   transmitted in the current generation. A request whose connection ended before its answer is
//!   `indeterminate`: it is never sent again.
//! - A select, or an interrogation or execution, is confirmed by its activation confirmation (7).
//!   An execution, and an interrogation, complete with the activation termination (10); the
//!   confirmation alone is only the activation. A negative confirmation, or a cause of 44 to 47,
//!   refuses the request.
//! - A request still waiting when its deadline passes is `timed_out`. A cancelled request is
//!   `cancelled`; if it was already written, its record says it was sent.
//! - Two requests cannot be told apart by their answers when they share a type identification and
//!   an information object address, so such a request is refused while the other is open. The number
//!   of open requests is bounded, so the client's command channel never fills (see `MAX_IN_FLIGHT`).

use std::time::{Duration, Instant};

use igs_iec104::answer::{judge, Answer, Request as Expect};
use igs_iec104::Command;
use igs_iec104_codec::asdu::Asdu;
use igs_iec104_codec::generated::profile::TypeId;
use igs_iec104_codec::header::{cause, CommonAddress, InformationObjectAddress};

/// The most requests open at a time. It is below the client's command channel (its capacity is
/// `CLIENT_CAPACITY`), so that a request never waits for room in that channel while the owner is
/// unable to read the events that would free it.
pub const MAX_IN_FLIGHT: usize = 8;

/// The number of finished requests kept for inspection. Open requests are always kept.
const RETAINED: usize = 256;

/// The identity of a request, unique within the runtime.
pub type RequestId = u64;

/// What a request asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A general interrogation (C_IC_NA_1).
    Interrogation,
    /// A counter interrogation (C_CI_NA_1): the general request, or a group of counters.
    CounterInterrogation,
    /// A single command (C_SC_NA_1): a select, or an execution.
    SingleCommand {
        /// True for a select (S/E = 1), false for an execution.
        select: bool,
    },
}

impl Kind {
    /// The cause that completes the request: the activation confirmation for a select, the
    /// activation termination otherwise.
    fn completes_at(self) -> u8 {
        match self {
            Self::SingleCommand { select: true } => cause::ACTIVATION_CONFIRMATION,
            Self::Interrogation
            | Self::CounterInterrogation
            | Self::SingleCommand { select: false } => cause::ACTIVATION_TERMINATION,
        }
    }

    /// The name of the kind, as the control API reports it.
    pub fn name(self) -> &'static str {
        match self {
            Self::Interrogation => "interrogation",
            Self::CounterInterrogation => "counter_interrogation",
            Self::SingleCommand { select: true } => "single_command_select",
            Self::SingleCommand { select: false } => "single_command_execute",
        }
    }
}

/// How a request ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// The station confirmed the request, and completed it.
    Confirmed,
    /// The station refused the request: a negative confirmation (`negative`), or a cause of 44 to 47.
    Refused {
        /// The cause of the station's answer.
        cause: u8,
        /// True for a negative confirmation.
        negative: bool,
    },
    /// No complete answer came before the deadline.
    TimedOut,
    /// The controller cancelled the request before it was answered.
    Cancelled,
    /// The connection ended after the request was written and before its answer.
    Indeterminate,
    /// The client discarded the request without a connection. It was not written.
    NotSent,
}

impl Outcome {
    /// The name of the outcome, as the control API reports it.
    pub fn name(self) -> &'static str {
        match self {
            Self::Confirmed => "confirmed",
            Self::Refused { .. } => "refused",
            Self::TimedOut => "timed_out",
            Self::Cancelled => "cancelled",
            Self::Indeterminate => "indeterminate",
            Self::NotSent => "not_sent",
        }
    }
}

/// A request that ended, reported once.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Finished {
    /// The request.
    pub id: RequestId,
    /// How it ended.
    pub outcome: Outcome,
    /// Whether its I frame was written to the connection.
    pub sent: bool,
    /// The connection generation of its transmission, when it was written.
    pub generation: Option<u64>,
}

/// Why a request was not accepted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubmitError {
    /// Another open request has the same type and address, so its answer could be confused with
    /// this one's.
    Busy(RequestId),
    /// `MAX_IN_FLIGHT` requests are open.
    TooManyOpen,
    /// The ASDU has no information object, so its answer cannot be recognised.
    Malformed,
}

/// Why a request could not be cancelled.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CancelError {
    /// No request has this identity.
    Unknown,
    /// The request already ended.
    Ended(Outcome),
}

/// The lifecycle of a request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    /// Accepted, not yet written.
    Queued,
    /// Written: waiting for the activation confirmation.
    Transmitted,
    /// Confirmed as activated: waiting for the activation termination.
    Activated,
    /// Ended.
    Ended(Outcome),
}

#[derive(Clone, Debug)]
struct Entry {
    id: RequestId,
    kind: Kind,
    asdu: Asdu,
    type_id: TypeId,
    common_address: CommonAddress,
    address: InformationObjectAddress,
    stage: Stage,
    sent: bool,
    generation: Option<u64>,
    /// True while the client holds the ASDU and has not reported it written or discarded.
    with_client: bool,
    deadline: Instant,
}

impl Entry {
    fn open(&self) -> bool {
        !matches!(self.stage, Stage::Ended(_))
    }

    fn finished(&self) -> Option<Finished> {
        match self.stage {
            Stage::Ended(outcome) => Some(Finished {
                id: self.id,
                outcome,
                sent: self.sent,
                generation: self.generation,
            }),
            _ => None,
        }
    }
}

/// A snapshot of a request, for inspection.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct View {
    /// The request.
    pub id: RequestId,
    /// What it asks for.
    pub kind: Kind,
    /// The type identification of the ASDU.
    pub type_id: TypeId,
    /// The information object address of the ASDU.
    pub address: InformationObjectAddress,
    /// `queued`, `transmitted`, `activated`, or `ended`.
    pub stage: &'static str,
    /// Whether its I frame was written.
    pub sent: bool,
    /// The connection generation of its transmission, when it was written.
    pub generation: Option<u64>,
    /// How it ended, when it did.
    pub outcome: Option<Outcome>,
}

/// The open and recent requests, and the connection generation they belong to.
#[derive(Debug)]
pub struct Correlator {
    generation: u64,
    next_id: RequestId,
    entries: Vec<Entry>,
}

impl Default for Correlator {
    fn default() -> Self {
        Self::new()
    }
}

impl Correlator {
    /// An empty correlator, before the first connection.
    pub fn new() -> Self {
        Self {
            generation: 0,
            next_id: 1,
            entries: Vec::new(),
        }
    }

    /// The connection generation: the number of connections so far.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Accepts a request for `asdu`, due within `timeout`. The client sends the ASDU; the request
    /// is judged by its type, common address, and address.
    pub fn submit(
        &mut self,
        kind: Kind,
        asdu: Asdu,
        now: Instant,
        timeout: Duration,
    ) -> Result<RequestId, SubmitError> {
        let type_id = asdu.type_id();
        let Some(address) = asdu.body.first_address() else {
            return Err(SubmitError::Malformed);
        };
        let open = self.entries.iter().filter(|entry| entry.open());
        if let Some(busy) = open
            .clone()
            .find(|entry| entry.type_id == type_id && entry.address == address)
        {
            return Err(SubmitError::Busy(busy.id));
        }
        if open.count() >= MAX_IN_FLIGHT {
            return Err(SubmitError::TooManyOpen);
        }
        let id = self.next_id;
        self.next_id += 1;
        let common_address = asdu.common_address;
        self.entries.push(Entry {
            id,
            kind,
            asdu,
            type_id,
            common_address,
            address,
            stage: Stage::Queued,
            sent: false,
            generation: None,
            with_client: true,
            deadline: now + timeout,
        });
        self.prune();
        Ok(id)
    }

    /// A connection was established: a new generation begins.
    pub fn connected(&mut self) {
        self.generation += 1;
    }

    /// The connection ended. A request written on it and not answered is `indeterminate`; it is
    /// not sent again. Returns the requests that ended.
    pub fn disconnected(&mut self) -> Vec<Finished> {
        let mut ended = Vec::new();
        for entry in &mut self.entries {
            if entry.sent && matches!(entry.stage, Stage::Transmitted | Stage::Activated) {
                entry.stage = Stage::Ended(Outcome::Indeterminate);
                ended.extend(entry.finished());
            }
        }
        ended
    }

    /// The client wrote an I frame with `asdu`: the first request holding it is now transmitted in
    /// the current generation.
    pub fn transmitted(&mut self, asdu: &Asdu) {
        let generation = self.generation;
        let Some(entry) = self
            .entries
            .iter_mut()
            .find(|entry| entry.with_client && entry.asdu == *asdu)
        else {
            return;
        };
        entry.with_client = false;
        entry.sent = true;
        if entry.stage == Stage::Queued {
            entry.stage = Stage::Transmitted;
            entry.generation = Some(generation);
        }
    }

    /// The client discarded a request without a connection. Returns the requests that ended.
    pub fn discarded(&mut self, command: &Command) -> Vec<Finished> {
        let Command::SendAsdu(asdu) = command else {
            return Vec::new();
        };
        let Some(entry) = self
            .entries
            .iter_mut()
            .find(|entry| entry.with_client && entry.asdu == *asdu)
        else {
            return Vec::new();
        };
        entry.with_client = false;
        if entry.stage == Stage::Queued {
            entry.stage = Stage::Ended(Outcome::NotSent);
            return entry.finished().into_iter().collect();
        }
        Vec::new()
    }

    /// Judges an ASDU of the station against the requests transmitted in the current generation.
    /// Returns the requests that ended.
    pub fn answer(&mut self, asdu: &Asdu) -> Vec<Finished> {
        let generation = self.generation;
        let mut ended = Vec::new();
        for entry in &mut self.entries {
            if entry.generation != Some(generation) {
                continue;
            }
            let expected = match entry.stage {
                Stage::Transmitted => cause::ACTIVATION_CONFIRMATION,
                Stage::Activated => cause::ACTIVATION_TERMINATION,
                Stage::Queued | Stage::Ended(_) => continue,
            };
            let expect = Expect {
                types: Some(std::slice::from_ref(&entry.type_id)),
                common_address: entry.common_address,
                address: entry.address,
                cause: expected,
            };
            match judge(asdu, &expect) {
                None => continue,
                Some(Answer::Refused { cause, negative }) => {
                    entry.stage = Stage::Ended(Outcome::Refused { cause, negative });
                }
                Some(Answer::Confirmed) => {
                    entry.stage = match (entry.stage, entry.kind.completes_at()) {
                        (Stage::Transmitted, cause::ACTIVATION_CONFIRMATION) => {
                            Stage::Ended(Outcome::Confirmed)
                        }
                        (Stage::Transmitted, _) => Stage::Activated,
                        _ => Stage::Ended(Outcome::Confirmed),
                    };
                }
            }
            ended.extend(entry.finished());
            break;
        }
        ended
    }

    /// Ends the requests whose deadline has passed. Returns them.
    pub fn expire(&mut self, now: Instant) -> Vec<Finished> {
        let mut ended = Vec::new();
        for entry in &mut self.entries {
            if entry.open() && entry.deadline <= now {
                entry.stage = Stage::Ended(Outcome::TimedOut);
                ended.extend(entry.finished());
            }
        }
        ended
    }

    /// The instant the next open request is due, if any.
    pub fn next_deadline(&self) -> Option<Instant> {
        self.entries
            .iter()
            .filter(|entry| entry.open())
            .map(|entry| entry.deadline)
            .min()
    }

    /// Cancels an open request. Returns how it ended.
    pub fn cancel(&mut self, id: RequestId) -> Result<Finished, CancelError> {
        let entry = self
            .entries
            .iter_mut()
            .find(|entry| entry.id == id)
            .ok_or(CancelError::Unknown)?;
        if let Stage::Ended(outcome) = entry.stage {
            return Err(CancelError::Ended(outcome));
        }
        entry.stage = Stage::Ended(Outcome::Cancelled);
        Ok(entry.finished().unwrap_or(Finished {
            id,
            outcome: Outcome::Cancelled,
            sent: entry.sent,
            generation: entry.generation,
        }))
    }

    /// A snapshot of a request, when it is open or recent.
    pub fn view(&self, id: RequestId) -> Option<View> {
        let entry = self.entries.iter().find(|entry| entry.id == id)?;
        let (stage, outcome) = match entry.stage {
            Stage::Queued => ("queued", None),
            Stage::Transmitted => ("transmitted", None),
            Stage::Activated => ("activated", None),
            Stage::Ended(outcome) => ("ended", Some(outcome)),
        };
        Some(View {
            id,
            kind: entry.kind,
            type_id: entry.type_id,
            address: entry.address,
            stage,
            sent: entry.sent,
            generation: entry.generation,
            outcome,
        })
    }

    /// Drops the oldest finished requests beyond the number kept. Open requests stay.
    fn prune(&mut self) {
        let finished = self.entries.iter().filter(|entry| !entry.open()).count();
        let mut excess = finished.saturating_sub(RETAINED);
        self.entries.retain(|entry| {
            if excess > 0 && !entry.open() && !entry.with_client {
                excess -= 1;
                return false;
            }
            true
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use igs_iec104::procedures;
    use igs_iec104_codec::elements::{CounterFreeze, Qcc, Qoc, Qoi, Sco};

    const STATION: CommonAddress = CommonAddress::new(1);
    const TIMEOUT: Duration = Duration::from_secs(10);

    fn at(value: u32) -> InformationObjectAddress {
        InformationObjectAddress::new(value).expect("an address in range")
    }

    fn interrogation() -> Asdu {
        procedures::interrogation(STATION, Qoi::STATION).expect("builds")
    }

    fn command(address: u32, select: bool) -> Asdu {
        let sco = Sco {
            on: true,
            qoc: Qoc::new(0, select).expect("a qualifier"),
        };
        procedures::single_command(STATION, at(address), sco, None).expect("builds")
    }

    /// The station's answer with `cause` and the P/N bit, for the request `asdu`.
    fn answer_to(asdu: &Asdu, cause: u8, negative: bool) -> Asdu {
        let mut cot = CauseOfTransmission::new(cause).expect("a cause");
        cot.negative = negative;
        Asdu {
            cot,
            common_address: asdu.common_address,
            body: asdu.body.clone(),
        }
    }

    use igs_iec104_codec::header::CauseOfTransmission;

    /// Submits `asdu` and writes it, in the current generation.
    fn sent(correlator: &mut Correlator, kind: Kind, asdu: Asdu, now: Instant) -> RequestId {
        let id = correlator
            .submit(kind, asdu.clone(), now, TIMEOUT)
            .expect("accepted");
        correlator.transmitted(&asdu);
        id
    }

    fn select() -> Kind {
        Kind::SingleCommand { select: true }
    }

    fn execute() -> Kind {
        Kind::SingleCommand { select: false }
    }

    #[test]
    fn a_selection_is_confirmed_by_its_activation_confirmation() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, true);
        let id = sent(&mut correlator, select(), asdu.clone(), now);
        let ended = correlator.answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false));
        assert_eq!(
            ended,
            vec![Finished {
                id,
                outcome: Outcome::Confirmed,
                sent: true,
                generation: Some(1),
            }]
        );
    }

    #[test]
    fn an_execution_is_confirmed_only_by_its_termination() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, false);
        let id = sent(&mut correlator, execute(), asdu.clone(), now);
        let confirmation = answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false);
        assert!(correlator.answer(&confirmation).is_empty());
        assert_eq!(correlator.view(id).map(|v| v.stage), Some("activated"));
        let termination = answer_to(&asdu, cause::ACTIVATION_TERMINATION, false);
        let ended = correlator.answer(&termination);
        assert_eq!(ended.len(), 1);
        assert_eq!(ended[0].outcome, Outcome::Confirmed);
    }

    #[test]
    fn an_interrogation_completes_with_its_termination() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = interrogation();
        let id = sent(&mut correlator, Kind::Interrogation, asdu.clone(), now);
        assert!(correlator
            .answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false))
            .is_empty());
        let ended = correlator.answer(&answer_to(&asdu, cause::ACTIVATION_TERMINATION, false));
        assert_eq!(ended[0].id, id);
        assert_eq!(ended[0].outcome, Outcome::Confirmed);
    }

    #[test]
    fn a_counter_interrogation_completes_with_its_termination() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let request = Qcc::new(5, CounterFreeze::Read).expect("a request");
        let asdu = procedures::counter_interrogation(STATION, request).expect("builds");
        let id = sent(
            &mut correlator,
            Kind::CounterInterrogation,
            asdu.clone(),
            now,
        );
        assert!(correlator
            .answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false))
            .is_empty());
        let ended = correlator.answer(&answer_to(&asdu, cause::ACTIVATION_TERMINATION, false));
        assert_eq!(ended[0].id, id);
        assert_eq!(ended[0].outcome, Outcome::Confirmed);
    }

    #[test]
    fn a_negative_confirmation_refuses_the_request() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, true);
        sent(&mut correlator, select(), asdu.clone(), now);
        let ended = correlator.answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, true));
        assert_eq!(
            ended[0].outcome,
            Outcome::Refused {
                cause: cause::ACTIVATION_CONFIRMATION,
                negative: true,
            }
        );
    }

    #[test]
    fn every_cause_from_44_to_47_refuses_the_request() {
        for refusal in 44..=47 {
            let now = Instant::now();
            let mut correlator = Correlator::new();
            correlator.connected();
            let asdu = command(5, true);
            sent(&mut correlator, select(), asdu.clone(), now);
            let ended = correlator.answer(&answer_to(&asdu, refusal, false));
            assert_eq!(
                ended[0].outcome,
                Outcome::Refused {
                    cause: refusal,
                    negative: false,
                },
                "cause {refusal}"
            );
        }
    }

    #[test]
    fn an_answer_of_another_cause_or_address_is_not_the_answer() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, false);
        sent(&mut correlator, execute(), asdu.clone(), now);
        // Termination before the confirmation, and an answer about another address.
        assert!(correlator
            .answer(&answer_to(&asdu, cause::ACTIVATION_TERMINATION, false))
            .is_empty());
        assert!(correlator
            .answer(&answer_to(
                &command(6, false),
                cause::ACTIVATION_CONFIRMATION,
                false
            ))
            .is_empty());
    }

    #[test]
    fn a_request_past_its_deadline_times_out() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        let id = correlator
            .submit(select(), command(5, true), now, TIMEOUT)
            .expect("accepted");
        assert!(correlator
            .expire(now + TIMEOUT - Duration::from_millis(1))
            .is_empty());
        let ended = correlator.expire(now + TIMEOUT);
        assert_eq!(
            ended,
            vec![Finished {
                id,
                outcome: Outcome::TimedOut,
                sent: false,
                generation: None,
            }]
        );
    }

    #[test]
    fn a_cancelled_request_reports_whether_it_was_written() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, true);
        let id = correlator
            .submit(select(), asdu.clone(), now, TIMEOUT)
            .expect("accepted");
        // Cancelled while the client still holds the ASDU: the write comes later.
        let finished = correlator.cancel(id).expect("cancelled");
        assert_eq!(
            (finished.outcome, finished.sent),
            (Outcome::Cancelled, false)
        );
        correlator.transmitted(&asdu);
        let view = correlator.view(id).expect("kept");
        assert_eq!(view.outcome, Some(Outcome::Cancelled));
        assert!(view.sent, "the write is recorded, so the record is honest");
        assert_eq!(
            correlator.cancel(id),
            Err(CancelError::Ended(Outcome::Cancelled))
        );
        assert_eq!(correlator.cancel(999), Err(CancelError::Unknown));
    }

    #[test]
    fn a_request_discarded_without_a_connection_was_not_sent() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        let asdu = command(5, true);
        let id = correlator
            .submit(select(), asdu.clone(), now, TIMEOUT)
            .expect("accepted");
        let ended = correlator.discarded(&Command::SendAsdu(asdu));
        assert_eq!(
            ended,
            vec![Finished {
                id,
                outcome: Outcome::NotSent,
                sent: false,
                generation: None,
            }]
        );
    }

    #[test]
    fn a_request_written_on_a_connection_that_ends_is_indeterminate_and_not_resent() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, true);
        let id = sent(&mut correlator, select(), asdu.clone(), now);
        let ended = correlator.disconnected();
        assert_eq!(
            ended,
            vec![Finished {
                id,
                outcome: Outcome::Indeterminate,
                sent: true,
                generation: Some(1),
            }]
        );
        // A later connection and a late answer do not complete it.
        correlator.connected();
        assert!(correlator
            .answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false))
            .is_empty());
        assert_eq!(
            correlator.view(id).and_then(|v| v.outcome),
            Some(Outcome::Indeterminate)
        );
    }

    #[test]
    fn an_answer_is_judged_only_in_the_generation_of_its_transmission() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        let asdu = command(5, true);
        // Accepted in generation 1, written in generation 2.
        let id = correlator
            .submit(select(), asdu.clone(), now, TIMEOUT)
            .expect("accepted");
        correlator.connected();
        correlator.transmitted(&asdu);
        assert_eq!(correlator.view(id).and_then(|v| v.generation), Some(2));
        let ended = correlator.answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false));
        assert_eq!(ended[0].generation, Some(2));
    }

    #[test]
    fn a_second_request_to_the_same_address_is_refused_while_the_first_is_open() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        let first = correlator
            .submit(select(), command(5, true), now, TIMEOUT)
            .expect("accepted");
        assert_eq!(
            correlator.submit(execute(), command(5, false), now, TIMEOUT),
            Err(SubmitError::Busy(first))
        );
        assert!(correlator
            .submit(execute(), command(6, false), now, TIMEOUT)
            .is_ok());
    }

    #[test]
    fn the_number_of_open_requests_is_bounded() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        for address in 1..=MAX_IN_FLIGHT as u32 {
            correlator
                .submit(select(), command(address, true), now, TIMEOUT)
                .expect("accepted");
        }
        assert_eq!(
            correlator.submit(select(), command(100, true), now, TIMEOUT),
            Err(SubmitError::TooManyOpen)
        );
        correlator.expire(now + TIMEOUT);
        assert!(correlator
            .submit(select(), command(100, true), now + TIMEOUT, TIMEOUT)
            .is_ok());
    }

    #[test]
    fn the_next_deadline_is_the_earliest_open_one() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        assert_eq!(correlator.next_deadline(), None);
        correlator
            .submit(select(), command(1, true), now, Duration::from_secs(5))
            .expect("accepted");
        correlator
            .submit(select(), command(2, true), now, Duration::from_secs(2))
            .expect("accepted");
        assert_eq!(
            correlator.next_deadline(),
            Some(now + Duration::from_secs(2))
        );
    }

    #[test]
    fn finished_requests_are_kept_for_inspection_up_to_a_bound() {
        let now = Instant::now();
        let mut correlator = Correlator::new();
        correlator.connected();
        for address in 1..=(RETAINED as u32 + 50) {
            let asdu = command(address, true);
            sent(&mut correlator, select(), asdu.clone(), now);
            correlator.answer(&answer_to(&asdu, cause::ACTIVATION_CONFIRMATION, false));
        }
        assert!(
            correlator.view(1).is_none(),
            "the oldest finished request is dropped"
        );
        assert!(correlator.view(RETAINED as u64 + 50).is_some());
    }

    #[test]
    fn a_write_with_no_request_holding_it_changes_nothing() {
        let mut correlator = Correlator::new();
        correlator.transmitted(&command(9, true));
        assert!(correlator
            .discarded(&Command::SendAsdu(command(9, true)))
            .is_empty());
        assert_eq!(correlator.generation(), 0);
    }
}

//! The client's point image: the last value the station sent for each point, with its quality,
//! its time tag, its cause, the time it was observed, and the connection generation it arrived in
//! (task C2, IMPLEMENTATION.md §7).
//!
//! It covers the single-point, double-point, and measured-value types, plain and time-tagged, as the
//! profile of the station defines them. An ASDU of another type changes nothing.

use std::collections::BTreeMap;

use igs_iec104::process_image::PointValue;
use igs_iec104_codec::asdu::{Asdu, Body, Objects, Timed};
use igs_iec104_codec::elements::{Bcr, Bsi, Diq, Nva, Qds, QualityFlags, Scd, Siq, Vti};
use igs_iec104_codec::formats::Cp56Time2a;
use igs_iec104_codec::generated::profile::TypeId;

/// The quality of a value: the flags of the quality descriptor, and the overflow of a measured value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Quality {
    /// IV: the value is invalid.
    pub invalid: bool,
    /// NT: the value was not updated in its interval.
    pub not_topical: bool,
    /// SB: the value was substituted.
    pub substituted: bool,
    /// BL: the value is blocked for transmission.
    pub blocked: bool,
    /// OV: the measured value overflowed. Always false for a point without it.
    pub overflow: bool,
}

impl Quality {
    fn of(flags: QualityFlags, overflow: bool) -> Self {
        Self {
            invalid: flags.invalid,
            not_topical: flags.not_topical,
            substituted: flags.substituted,
            blocked: flags.blocked,
            overflow,
        }
    }
}

/// One value of an ASDU, before it is stored.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Observed {
    /// The value, in the form of its kind.
    pub value: PointValue,
    /// The quality of the value.
    pub quality: Quality,
    /// The time tag, for a time-tagged type.
    pub time: Option<Cp56Time2a>,
}

/// The last value of a point, with what the station said about it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Point {
    /// The value, in the form of its kind.
    pub value: PointValue,
    /// The type identification of the ASDU that carried it.
    pub type_id: TypeId,
    /// The cause of transmission of that ASDU.
    pub cause: u8,
    /// The quality of the value.
    pub quality: Quality,
    /// The time tag, for a time-tagged type.
    pub time: Option<Cp56Time2a>,
    /// The time the value was received, in milliseconds since the Unix epoch (UTC).
    pub observed_at_ms: u64,
    /// The connection generation the value arrived in.
    pub generation: u64,
}

/// The points of the station, by information object address.
#[derive(Debug, Default)]
pub struct PointImage {
    points: BTreeMap<u32, Point>,
}

impl PointImage {
    /// Stores the values of an ASDU received in `generation` at `observed_at_ms`. Returns how many
    /// values it stored.
    pub fn observe(&mut self, asdu: &Asdu, generation: u64, observed_at_ms: u64) -> usize {
        let observed = observations(asdu);
        let count = observed.len();
        for (address, value) in observed {
            self.points.insert(
                address,
                Point {
                    value: value.value,
                    type_id: asdu.type_id(),
                    cause: asdu.cot.cause(),
                    quality: value.quality,
                    time: value.time,
                    observed_at_ms,
                    generation,
                },
            );
        }
        count
    }

    /// The last value of the point at `address`.
    pub fn get(&self, address: u32) -> Option<&Point> {
        self.points.get(&address)
    }

    /// The points, by address.
    pub fn iter(&self) -> impl Iterator<Item = (u32, &Point)> {
        self.points.iter().map(|(address, point)| (*address, point))
    }
}

/// The values an ASDU carries, by address. An ASDU of a type the image does not hold gives none.
pub fn observations(asdu: &Asdu) -> Vec<(u32, Observed)> {
    match &asdu.body {
        Body::M_SP_NA_1(objects) => plain(objects, |siq| single(siq, None)),
        Body::M_SP_TB_1(objects) => stamped(objects, |siq| single(siq, None)),
        Body::M_DP_NA_1(objects) => plain(objects, |diq| double(diq, None)),
        Body::M_DP_TB_1(objects) => stamped(objects, |diq| double(diq, None)),
        Body::M_ME_NA_1(objects) => plain(objects, |(nva, qds)| {
            measured(PointValue::Normalized(nva, qds), qds, None)
        }),
        Body::M_ME_TD_1(objects) => stamped(objects, |(nva, qds)| {
            measured(PointValue::Normalized(nva, qds), qds, None)
        }),
        Body::M_ME_NB_1(objects) => plain(objects, |(sva, qds)| {
            measured(PointValue::Scaled(sva, qds), qds, None)
        }),
        Body::M_ME_TE_1(objects) => stamped(objects, |(sva, qds)| {
            measured(PointValue::Scaled(sva, qds), qds, None)
        }),
        Body::M_ME_NC_1(objects) => plain(objects, |(float, qds)| {
            measured(PointValue::Float(float, qds), qds, None)
        }),
        Body::M_ME_TF_1(objects) => stamped(objects, |(float, qds)| {
            measured(PointValue::Float(float, qds), qds, None)
        }),
        Body::M_IT_NA_1(objects) => plain(objects, |bcr| counter(bcr, None)),
        Body::M_IT_TB_1(objects) => stamped(objects, |bcr| counter(bcr, None)),
        Body::M_ST_NA_1(objects) => plain(objects, |(vti, qds)| step(vti, qds, None)),
        Body::M_ST_TB_1(objects) => stamped(objects, |(vti, qds)| step(vti, qds, None)),
        Body::M_BO_NA_1(objects) => plain(objects, |(bsi, qds)| bitstring(bsi, qds, None)),
        Body::M_BO_TB_1(objects) => stamped(objects, |(bsi, qds)| bitstring(bsi, qds, None)),
        Body::M_PS_NA_1(objects) => plain(objects, |(scd, qds)| packed(scd, qds)),
        Body::M_ME_ND_1(objects) => plain(objects, normalized_unqualified),
        _ => Vec::new(),
    }
}

/// The values of a plain type, each with its own conversion.
fn plain<T: Clone>(objects: &Objects<T>, convert: impl Fn(T) -> Observed) -> Vec<(u32, Observed)> {
    items(objects)
        .into_iter()
        .map(|(address, value)| (address, convert(value)))
        .collect()
}

/// The values of a time-tagged type. The time tag comes with each value.
fn stamped<T: Clone>(
    objects: &Objects<Timed<T>>,
    convert: impl Fn(T) -> Observed,
) -> Vec<(u32, Observed)> {
    items(objects)
        .into_iter()
        .map(|(address, timed)| {
            let mut observed = convert(timed.value);
            observed.time = Some(timed.time);
            (address, observed)
        })
        .collect()
}

/// The (address, value) pairs of an object list, in the order they were sent. A sequence has one
/// address and consecutive values from it.
fn items<T: Clone>(objects: &Objects<T>) -> Vec<(u32, T)> {
    match objects {
        Objects::Individual(list) => list
            .iter()
            .map(|item| (item.address.value(), item.value.clone()))
            .collect(),
        Objects::Sequence { address, values } => values
            .iter()
            .enumerate()
            .filter_map(|(index, value)| {
                let offset = u32::try_from(index).ok()?;
                Some((address.value().checked_add(offset)?, value.clone()))
            })
            .collect(),
    }
}

fn single(siq: Siq, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value: PointValue::Single(siq),
        quality: Quality::of(siq.quality, false),
        time,
    }
}

fn double(diq: Diq, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value: PointValue::Double(diq),
        quality: Quality::of(diq.quality, false),
        time,
    }
}

/// A counter reading. Its invalid flag is the quality; its carry and adjusted flags stay with the
/// value, which keeps them.
fn counter(bcr: Bcr, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value: PointValue::Counter(bcr),
        quality: Quality {
            invalid: bcr.invalid,
            not_topical: false,
            substituted: false,
            blocked: false,
            overflow: false,
        },
        time,
    }
}

fn step(vti: Vti, qds: Qds, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value: PointValue::Step(vti, qds),
        quality: Quality::of(qds.quality, false),
        time,
    }
}

fn bitstring(bits: Bsi, qds: Qds, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value: PointValue::Bitstring(bits, qds),
        quality: Quality::of(qds.quality, false),
        time,
    }
}

fn packed(bits: Scd, qds: Qds) -> Observed {
    Observed {
        value: PointValue::Packed(bits, qds),
        quality: Quality::of(qds.quality, false),
        time: None,
    }
}

/// A normalized value without quality descriptor: it has no quality flags, so none is set.
fn normalized_unqualified(nva: Nva) -> Observed {
    Observed {
        value: PointValue::NormalizedUnqualified(nva),
        quality: Quality {
            invalid: false,
            not_topical: false,
            substituted: false,
            blocked: false,
            overflow: false,
        },
        time: None,
    }
}

fn measured(value: PointValue, qds: Qds, time: Option<Cp56Time2a>) -> Observed {
    Observed {
        value,
        quality: Quality::of(qds.quality, qds.overflow),
        time,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use igs_iec104_codec::asdu::InformationObject;
    use igs_iec104_codec::elements::{DoublePoint, Nva, QualityFlags, ShortFloat, Sva};
    use igs_iec104_codec::header::{
        cause, CauseOfTransmission, CommonAddress, InformationObjectAddress,
    };

    fn at(value: u32) -> InformationObjectAddress {
        InformationObjectAddress::new(value).expect("an address in range")
    }

    fn asdu(cause_code: u8, body: Body) -> Asdu {
        Asdu {
            cot: CauseOfTransmission::new(cause_code).expect("a cause"),
            common_address: CommonAddress::new(1),
            body,
        }
    }

    fn individual<T>(items: Vec<(u32, T)>) -> Objects<T> {
        Objects::Individual(
            items
                .into_iter()
                .map(|(address, value)| InformationObject {
                    address: at(address),
                    value,
                })
                .collect(),
        )
    }

    fn time() -> Cp56Time2a {
        Cp56Time2a::new(1_500, 30, 12, 11, 0, 10, 26).expect("a valid time tag")
    }

    #[test]
    fn a_counter_reading_is_observed_with_its_invalid_flag() {
        let reading = Bcr::new(-42, 3).expect("a counter");
        let mut invalid = Bcr::new(9, 0).expect("a counter");
        invalid.invalid = true;
        let observed = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_IT_NA_1(individual(vec![(7, reading), (8, invalid)])),
        ));
        assert_eq!(observed.len(), 2);
        assert_eq!(observed[0].0, 7);
        assert_eq!(observed[0].1.value, PointValue::Counter(reading));
        assert!(!observed[0].1.quality.invalid);
        assert!(observed[1].1.quality.invalid);
        assert_eq!(observed[1].1.time, None);
    }

    #[test]
    fn a_step_and_a_bitstring_are_observed_with_their_time_tags() {
        let vti = Vti::new(-3, true).expect("a step");
        let mut bits = Bsi::new();
        bits = bits.with_bit(2, true).expect("bit 2");
        let steps = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_ST_NA_1(individual(vec![(5, (vti, Qds::default()))])),
        ));
        assert_eq!(steps[0].0, 5);
        assert_eq!(steps[0].1.value, PointValue::Step(vti, Qds::default()));
        assert_eq!(steps[0].1.time, None);
        let stamped = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_BO_TB_1(individual(vec![(
                6,
                Timed {
                    value: (bits, Qds::default()),
                    time: time(),
                },
            )])),
        ));
        assert_eq!(stamped[0].0, 6);
        assert_eq!(
            stamped[0].1.value,
            PointValue::Bitstring(bits, Qds::default())
        );
        assert!(stamped[0].1.time.is_some());
    }

    #[test]
    fn packed_and_unqualified_readings_are_observed_without_a_time_tag() {
        let mut bits = Scd::new().with_status(3, true).expect("point 3");
        bits = bits.with_changed(3, true).expect("point 3 changed");
        let packed = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_PS_NA_1(individual(vec![(8, (bits, Qds::default()))])),
        ));
        assert_eq!(packed[0].0, 8);
        assert_eq!(packed[0].1.value, PointValue::Packed(bits, Qds::default()));
        assert_eq!(packed[0].1.time, None);
        let nva = Nva::try_from_f32(0.5).expect("a normalized value");
        let plain = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_ME_ND_1(individual(vec![(9, nva)])),
        ));
        assert_eq!(plain[0].1.value, PointValue::NormalizedUnqualified(nva));
        assert!(!plain[0].1.quality.invalid);
    }

    fn on(on: bool) -> Siq {
        Siq {
            on,
            quality: QualityFlags::default(),
        }
    }

    #[test]
    fn a_single_point_is_stored_with_its_value_and_quality() {
        let flagged = Siq {
            on: true,
            quality: QualityFlags {
                invalid: true,
                ..QualityFlags::default()
            },
        };
        let observed = observations(&asdu(
            cause::INTERROGATED_STATION,
            Body::M_SP_NA_1(individual(vec![(1, on(false)), (2, flagged)])),
        ));
        assert_eq!(observed.len(), 2);
        assert_eq!(observed[0].0, 1);
        assert_eq!(observed[0].1.value, PointValue::Single(on(false)));
        assert!(!observed[0].1.quality.invalid);
        assert!(observed[1].1.quality.invalid);
        assert_eq!(observed[1].1.time, None);
    }

    #[test]
    fn a_time_tagged_point_keeps_its_time_tag() {
        let observed = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_SP_TB_1(individual(vec![(
                3,
                Timed {
                    value: on(true),
                    time: time(),
                },
            )])),
        ));
        assert_eq!(observed[0].1.time, Some(time()));
        assert_eq!(observed[0].1.value, PointValue::Single(on(true)));
    }

    #[test]
    fn a_double_point_keeps_its_state() {
        let diq = Diq {
            state: DoublePoint::On,
            quality: QualityFlags::default(),
        };
        let observed = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_DP_NA_1(individual(vec![(4, diq)])),
        ));
        assert_eq!(observed[0].1.value, PointValue::Double(diq));
    }

    #[test]
    fn a_measured_value_keeps_its_overflow_and_quality() {
        let qds = Qds {
            overflow: true,
            quality: QualityFlags::default(),
        };
        let nva = Nva::from_raw(16_384);
        let observed = observations(&asdu(
            cause::INTERROGATED_STATION,
            Body::M_ME_NA_1(individual(vec![(5, (nva, qds))])),
        ));
        assert_eq!(observed[0].1.value, PointValue::Normalized(nva, qds));
        assert!(observed[0].1.quality.overflow);
    }

    #[test]
    fn scaled_and_float_values_keep_their_form() {
        let sva = Sva::new(-700);
        let float = ShortFloat::from_f32(12.5);
        let qds = Qds::default();
        let scaled = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_ME_NB_1(individual(vec![(6, (sva, qds))])),
        ));
        assert_eq!(scaled[0].1.value, PointValue::Scaled(sva, qds));
        let stamped = observations(&asdu(
            cause::SPONTANEOUS,
            Body::M_ME_TF_1(individual(vec![(
                7,
                Timed {
                    value: (float, qds),
                    time: time(),
                },
            )])),
        ));
        assert_eq!(stamped[0].1.value, PointValue::Float(float, qds));
        assert_eq!(stamped[0].1.time, Some(time()));
    }

    #[test]
    fn a_sequence_gives_consecutive_addresses() {
        let objects = Objects::Sequence {
            address: at(10),
            values: vec![on(true), on(false)],
        };
        let observed = observations(&asdu(cause::INTERROGATED_STATION, Body::M_SP_NA_1(objects)));
        let addresses: Vec<u32> = observed.iter().map(|(address, _)| *address).collect();
        assert_eq!(addresses, vec![10, 11]);
    }

    #[test]
    fn an_asdu_of_another_type_changes_nothing() {
        let observed = observations(&asdu(
            cause::ACTIVATION_TERMINATION,
            Body::C_IC_NA_1(individual(vec![(
                0,
                igs_iec104_codec::elements::Qoi::STATION,
            )])),
        ));
        assert!(observed.is_empty());
    }

    #[test]
    fn the_image_keeps_the_last_value_with_its_generation() {
        let mut image = PointImage::default();
        image.observe(
            &asdu(
                cause::INTERROGATED_STATION,
                Body::M_SP_NA_1(individual(vec![(1, on(false))])),
            ),
            1,
            1_000,
        );
        let stored = image.observe(
            &asdu(
                cause::SPONTANEOUS,
                Body::M_SP_NA_1(individual(vec![(1, on(true))])),
            ),
            2,
            2_000,
        );
        assert_eq!(stored, 1);
        let point = image.get(1).expect("stored");
        assert_eq!(point.value, PointValue::Single(on(true)));
        assert_eq!(
            (point.cause, point.generation, point.observed_at_ms),
            (cause::SPONTANEOUS, 2, 2_000)
        );
        assert_eq!(image.iter().count(), 1);
    }
}

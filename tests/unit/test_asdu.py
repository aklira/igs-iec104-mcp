"""The single-point reader of the control API's ASDUs (task S3)."""

import pytest

from igs_iec104_mcp.runtime.asdu import SinglePoint, single_points


def asdu(hex_bytes: str, type_id: int = 1, cause: int = 20) -> dict[str, object]:
    return {"type_id": type_id, "cause": cause, "bytes_hex": hex_bytes}


def test_an_individual_object_gives_its_address_and_value() -> None:
    # Type 1, one object, cause 20, common address 1, address 1, SIQ with the value on.
    assert single_points(asdu("01011400010001000001")) == [
        SinglePoint(address=1, on=True)
    ]


def test_the_value_bit_is_the_low_bit_of_the_siq() -> None:
    assert single_points(asdu("01011400010001000000")) == [
        SinglePoint(address=1, on=False)
    ]
    # The quality bits above the value do not change the value.
    assert single_points(asdu("01011400010001000011")) == [
        SinglePoint(address=1, on=True)
    ]


def test_a_sequence_gives_consecutive_addresses() -> None:
    # Sequence form (qualifier bit set), two values from address 1: on, then off.
    assert single_points(asdu("0182140001000100000100")) == [
        SinglePoint(address=1, on=True),
        SinglePoint(address=2, on=False),
    ]


def test_another_type_has_no_single_points() -> None:
    assert single_points(asdu("0c011400010001000001", type_id=12)) == []


def test_a_truncated_encoding_is_refused() -> None:
    with pytest.raises(ValueError):
        single_points(asdu("010114000100"))
    with pytest.raises(ValueError):
        single_points(asdu("0101140001000100000101"))

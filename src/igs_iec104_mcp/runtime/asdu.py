"""Reads the single-point values of the ASDUs that the control API reports (task S3).

This is a spike reader for milestone 1. It handles only the single-point type
(M_SP_NA_1, type 1, without time tag), in its two structures. The point image of task C2
replaces it.

The single-point value is bit 0 of the SIQ octet (IEC 60870-5-101 §7.2.6.1, as the
upstream codec reads it). The header is the type identification, the variable structure
qualifier, the cause of transmission, and the common address."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SINGLE_POINT: int = 1
_HEADER: int = (
    6  # type identification, qualifier, cause (2 octets), common address (2 octets)
)
_SIQ_VALUE_BIT: int = 0x01
_SEQUENCE_BIT: int = 0x80
_COUNT_MASK: int = 0x7F


@dataclass(frozen=True)
class SinglePoint:
    """One single-point value of an ASDU."""

    address: int
    on: bool


def single_points(asdu: dict[str, Any]) -> list[SinglePoint]:
    """The single-point values of an ASDU of the control API, or none for another type.

    Raises ValueError when the encoding is shorter than its structure says it is.
    """
    if asdu.get("type_id") != SINGLE_POINT:
        return []
    raw = bytes.fromhex(str(asdu.get("bytes_hex") or ""))
    if len(raw) < _HEADER + 1:
        raise ValueError("a single-point ASDU has no information object")
    qualifier = raw[1]
    count = qualifier & _COUNT_MASK
    body = raw[_HEADER:]
    if qualifier & _SEQUENCE_BIT:
        return _sequence(body, count)
    return _individual(body, count)


def _individual(body: bytes, count: int) -> list[SinglePoint]:
    """Each object carries its own address and its value: three octets, then one."""
    if len(body) != count * 4:
        raise ValueError("the information objects do not match the count")
    points = []
    for index in range(count):
        start = index * 4
        address = int.from_bytes(body[start : start + 3], "little")
        points.append(
            SinglePoint(address=address, on=bool(body[start + 3] & _SIQ_VALUE_BIT))
        )
    return points


def _sequence(body: bytes, count: int) -> list[SinglePoint]:
    """One address, then consecutive values from it: three octets, then one each."""
    if len(body) != 3 + count:
        raise ValueError("the information objects do not match the count")
    first = int.from_bytes(body[0:3], "little")
    return [
        SinglePoint(address=first + index, on=bool(body[3 + index] & _SIQ_VALUE_BIT))
        for index in range(count)
    ]

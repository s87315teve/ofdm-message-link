"""Fixed modulation-and-coding table addressed by one wire index.

The layout follows the 3GPP idea used by LTE TS 36.213 table 7.1.7.1-1: one
small integer selects a complete transmission format, the table is grouped by
modulation order, and spectral efficiency increases within each group.  The
index is a configuration and telemetry name, not a new wire field: the burst
header already identifies the format with its modulation byte and its wire
version, so every entry simply names one existing (modulation, wire version)
pair and no negotiation or separate FEC identifier is needed.

Where this table deliberately differs from LTE: LTE reaches a near-continuum of
effective code rates by rate matching one mother code, while this project has
no rate matching.  Every entry here is a distinct mother code, so the rate
column is coarse and no rate between the listed values is reachable.  The index
space is intentionally left sparse so rate-matched entries can be added later
without renumbering the entries that already exist on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .codec import MCS
from .turbo import turbo_coded_bit_count

FecScheme = Literal["turbo", "convolutional", "uncoded"]

_CONVOLUTIONAL_TAIL_BITS = 6


class McsTableError(ValueError):
    """Raised when an index or format is not in the fixed table."""


@dataclass(frozen=True, slots=True)
class McsTableEntry:
    """One complete, immutable transmission format."""

    index: int
    modulation: MCS
    fec_scheme: FecScheme
    code_rate_inverse: int
    wire_version: int

    @property
    def code_rate(self) -> str:
        """Nominal rate as it appears in configuration and telemetry."""

        return "1" if self.code_rate_inverse == 1 else f"1/{self.code_rate_inverse}"

    @property
    def bits_per_symbol(self) -> int:
        """Modulation order of this entry."""

        return self.modulation.bits_per_symbol

    @property
    def spectral_efficiency(self) -> float:
        """Nominal information bits per constellation symbol.

        This is the table's ordering key.  Like the LTE column it names, it
        ignores tail bits, padding, and burst framing, so it ranks formats
        rather than predicting goodput.
        """

        return self.bits_per_symbol / self.code_rate_inverse

    def coded_bit_count(self, information_bit_count: int) -> int:
        """Return the exact transmitted length for this format."""

        if type(information_bit_count) is not int or information_bit_count < 0:
            raise McsTableError(
                "information_bit_count must be a non-negative integer"
            )
        if self.fec_scheme == "turbo":
            return turbo_coded_bit_count(information_bit_count)
        if self.fec_scheme == "uncoded":
            return information_bit_count
        return self.code_rate_inverse * (
            information_bit_count + _CONVOLUTIONAL_TAIL_BITS
        )


# Grouped by modulation order, then increasing spectral efficiency, so a higher
# index is always at least as fast and never more robust than a lower one in
# the same group.  Indices 0 and 4 reproduce the wire-v2 Turbo profile and
# indices 2 and 6 reproduce the wire-v1 convolutional profile.
MCS_TABLE: tuple[McsTableEntry, ...] = (
    McsTableEntry(0, MCS.QPSK, "turbo", 3, 2),
    McsTableEntry(1, MCS.QPSK, "convolutional", 3, 3),
    McsTableEntry(2, MCS.QPSK, "convolutional", 2, 1),
    McsTableEntry(3, MCS.QPSK, "uncoded", 1, 4),
    McsTableEntry(4, MCS.QAM16, "turbo", 3, 2),
    McsTableEntry(5, MCS.QAM16, "convolutional", 3, 3),
    McsTableEntry(6, MCS.QAM16, "convolutional", 2, 1),
    McsTableEntry(7, MCS.QAM16, "uncoded", 1, 4),
)

MAX_MCS_INDEX = MCS_TABLE[-1].index

_BY_INDEX = {entry.index: entry for entry in MCS_TABLE}
_BY_WIRE = {(entry.modulation, entry.wire_version): entry for entry in MCS_TABLE}
_BY_FORMAT = {
    (entry.modulation, entry.fec_scheme, entry.code_rate_inverse): entry
    for entry in MCS_TABLE
}


def mcs_entry(index: int) -> McsTableEntry:
    """Return the entry addressed by one wire index."""

    if type(index) is not int:
        raise McsTableError("mcs_index must be an integer")
    try:
        return _BY_INDEX[index]
    except KeyError:
        raise McsTableError(
            f"unknown mcs_index {index}; the table defines {sorted(_BY_INDEX)}"
        ) from None


def mcs_index_for(
    modulation: MCS,
    fec_scheme: FecScheme,
    code_rate: str,
) -> int:
    """Return the index naming one configured modulation and code rate."""

    if not isinstance(modulation, MCS):
        raise McsTableError("modulation must be an MCS")
    rate_inverse = 1 if code_rate == "1" else _parse_rate_inverse(code_rate)
    try:
        return _BY_FORMAT[(modulation, fec_scheme, rate_inverse)].index
    except KeyError:
        raise McsTableError(
            f"no table entry for {modulation.name} {fec_scheme} rate {code_rate}"
        ) from None


def _parse_rate_inverse(code_rate: str) -> int:
    if not isinstance(code_rate, str) or not code_rate.startswith("1/"):
        raise McsTableError(f"unsupported code_rate {code_rate!r}")
    try:
        return int(code_rate[2:])
    except ValueError:
        raise McsTableError(f"unsupported code_rate {code_rate!r}") from None


def mcs_entry_for_wire(modulation: MCS, wire_version: int) -> McsTableEntry:
    """Return the entry a received burst header describes."""

    if not isinstance(modulation, MCS):
        raise McsTableError("modulation must be an MCS")
    try:
        return _BY_WIRE[(modulation, wire_version)]
    except KeyError:
        raise McsTableError(
            f"no table entry for {modulation.name} wire version {wire_version}"
        ) from None


def mcs_index_for_wire(modulation: MCS, wire_version: int) -> int:
    """Return the table index a received or configured burst format uses."""

    return mcs_entry_for_wire(modulation, wire_version).index


def describe_mcs_entry(entry: McsTableEntry) -> str:
    """Return one compact human label, ordered like the LTE table columns."""

    scheme = {
        "turbo": "Turbo",
        "convolutional": "Conv",
        "uncoded": "Uncoded",
    }[entry.fec_scheme]
    modulation = "QPSK" if entry.modulation is MCS.QPSK else "16QAM"
    return (
        f"MCS {entry.index} — {modulation}, {scheme} r{entry.code_rate} "
        f"({entry.spectral_efficiency:.2f} b/sym)"
    )

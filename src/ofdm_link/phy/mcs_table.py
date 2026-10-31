"""Fixed modulation-and-coding table addressed by one wire index.

The layout follows the 3GPP idea used by LTE TS 36.213 table 7.1.7.1-1: one
small integer selects a complete transmission format, the table is grouped by
modulation order, and spectral efficiency increases within each group.

Indices 0-7 are the legacy (wire v1-v4) formats.  For them the index is a
configuration and telemetry name, not a wire field: the legacy burst header
identifies the format by its modulation byte and wire version, so each entry
names one (modulation, wire version) pair.  Each is a distinct mother code, so
their rate column is coarse.

Indices 8-17 are wire v5: one LTE Turbo mother code rate matched (TS 36.212
clause 5.1.4.1) onto an exact number of OFDM symbols, so any rate is
reachable.  Several v5 entries share one modulation, so the (modulation, wire
version) pair no longer identifies a format and the v5 SIG carries the index
itself.  The receiver keeps decoding the legacy entries; only v5 is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Literal

from .codec import MCS
from .rate_match import RateMatchPlan, rate_match_plan
from .turbo import turbo_coded_bit_count

FecScheme = Literal["turbo", "convolutional", "uncoded", "turbo_rm"]
McsFamily = Literal["legacy", "v5"]

V5_WIRE_VERSION = 5
# v5 keeps the legacy numerology (802.11a carrier map, 48 data carriers per
# OFDM symbol), so the payload grid is a constant here rather than config.
V5_DATA_CARRIERS = 48
V5_PSDU_FCS_BYTES = 4
V5_MAX_PSDU_BYTES = (1 << 16) - 1

_CONVOLUTIONAL_TAIL_BITS = 6


class McsTableError(ValueError):
    """Raised when an index or format is not in the fixed table."""


@dataclass(frozen=True, slots=True)
class McsTableEntry:
    """One complete, immutable transmission format.

    ``code_rate_inverse`` is the mother code: the transmitted rate of a legacy
    entry, and the Turbo coder's 1/3 for a rate-matched one.  ``rate`` is the
    nominal transmitted rate for both.  ``tx_visible`` is False for entries the
    receiver decodes but the transmit menu should not offer.
    """

    index: int
    modulation: MCS
    fec_scheme: FecScheme
    code_rate_inverse: int
    wire_version: int
    rate: Fraction
    tx_visible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Fraction) or not 0 < self.rate <= 1:
            raise McsTableError("rate must be a Fraction in (0, 1]")
        if self.fec_scheme == "turbo_rm":
            if self.wire_version != V5_WIRE_VERSION or self.code_rate_inverse != 3:
                raise McsTableError(
                    "turbo_rm entries are wire-v5 rate matchings of the 1/3 mother code"
                )
        elif self.rate != Fraction(1, self.code_rate_inverse):
            raise McsTableError("a legacy entry's rate is 1/code_rate_inverse")

    @property
    def family(self) -> McsFamily:
        """Which SIG format and receive path carry this entry."""

        return "v5" if self.wire_version == V5_WIRE_VERSION else "legacy"

    @property
    def code_rate(self) -> str:
        """Nominal rate as it appears in configuration and telemetry."""

        return _rate_label(self.rate)

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

        return float(self.bits_per_symbol * self.rate)

    def coded_bit_count(self, information_bit_count: int) -> int:
        """Return the exact transmitted length for this format.

        For a rate-matched entry this is ``G``: whole OFDM symbols, no pad.
        """

        if type(information_bit_count) is not int or information_bit_count < 0:
            raise McsTableError(
                "information_bit_count must be a non-negative integer"
            )
        if self.fec_scheme == "turbo_rm":
            return (
                V5_DATA_CARRIERS
                * self.bits_per_symbol
                * _v5_symbol_count(self, information_bit_count)
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
#
# ``MCS_TABLE`` keeps meaning the legacy table so that its existing callers
# (transmit menus, wire lookups) see no change until they opt into v5 through
# :func:`v5_entries`; :data:`ALL_MCS_ENTRIES` is the complete index space.
MCS_TABLE: tuple[McsTableEntry, ...] = (
    McsTableEntry(0, MCS.QPSK, "turbo", 3, 2, Fraction(1, 3)),
    McsTableEntry(1, MCS.QPSK, "convolutional", 3, 3, Fraction(1, 3)),
    McsTableEntry(2, MCS.QPSK, "convolutional", 2, 1, Fraction(1, 2)),
    McsTableEntry(3, MCS.QPSK, "uncoded", 1, 4, Fraction(1)),
    McsTableEntry(4, MCS.QAM16, "turbo", 3, 2, Fraction(1, 3)),
    McsTableEntry(5, MCS.QAM16, "convolutional", 3, 3, Fraction(1, 3)),
    McsTableEntry(6, MCS.QAM16, "convolutional", 2, 1, Fraction(1, 2)),
    McsTableEntry(7, MCS.QAM16, "uncoded", 1, 4, Fraction(1)),
)


def _v5(
    index: int,
    modulation: MCS,
    rate: Fraction,
    *,
    tx_visible: bool = True,
) -> McsTableEntry:
    return McsTableEntry(
        index, modulation, "turbo_rm", 3, V5_WIRE_VERSION, rate, tx_visible
    )


# Wire v5, same grouping rule.  Hidden entries stay decodable but are not
# offered for transmit: 11 has the spectral efficiency of 10 and only the
# better of the two will survive measurement; 64QAM needs an effective SNR
# above the Pluto's 20-22 dB EVM floor and stays a candidate until hardware
# shows otherwise.
V5_MCS_TABLE: tuple[McsTableEntry, ...] = (
    _v5(8, MCS.QPSK, Fraction(1, 3)),
    _v5(9, MCS.QPSK, Fraction(1, 2)),
    _v5(10, MCS.QPSK, Fraction(2, 3)),
    _v5(11, MCS.QAM16, Fraction(1, 3), tx_visible=False),
    _v5(12, MCS.QAM16, Fraction(1, 2)),
    _v5(13, MCS.QAM16, Fraction(2, 3)),
    _v5(14, MCS.QAM16, Fraction(3, 4)),
    _v5(15, MCS.QAM64, Fraction(1, 2), tx_visible=False),
    _v5(16, MCS.QAM64, Fraction(2, 3), tx_visible=False),
    _v5(17, MCS.QAM64, Fraction(3, 4), tx_visible=False),
)

ALL_MCS_ENTRIES: tuple[McsTableEntry, ...] = MCS_TABLE + V5_MCS_TABLE

MAX_MCS_INDEX = ALL_MCS_ENTRIES[-1].index

_BY_INDEX = {entry.index: entry for entry in ALL_MCS_ENTRIES}
# Wire lookups stay legacy-only: a v5 burst names its entry by index.
_BY_WIRE = {(entry.modulation, entry.wire_version): entry for entry in MCS_TABLE}
_BY_FORMAT = {
    (entry.modulation, entry.fec_scheme, entry.rate): entry
    for entry in ALL_MCS_ENTRIES
}


def v5_entries(*, include_hidden: bool = False) -> tuple[McsTableEntry, ...]:
    """Return the wire-v5 entries; by default only those offered for transmit."""

    if include_hidden:
        return V5_MCS_TABLE
    return tuple(entry for entry in V5_MCS_TABLE if entry.tx_visible)


@dataclass(frozen=True, slots=True)
class PayloadGeometry:
    """Everything both ends derive from (v5 entry, PSDU length).

    ``psdu_bytes`` includes the 4-byte FCS.  ``available_bits`` is ``G =
    48 Qm N_sym``, which rate matching fills exactly, so there are no pad bits.
    """

    psdu_bytes: int
    information_bits: int
    ofdm_symbol_count: int
    available_bits: int
    rate_match: RateMatchPlan

    @property
    def effective_rate(self) -> float:
        """Information bits per transmitted coded bit, ``B / G <= R``."""

        return self.information_bits / self.available_bits


def v5_payload_geometry(entry: McsTableEntry, psdu_bytes: int) -> PayloadGeometry:
    """Return the payload geometry of one v5 entry and PSDU length.

    ``N_sym = ceil(B / (R 48 Qm))`` is evaluated in integers, so transmitter
    and receiver cannot disagree through float rounding.
    """

    if not isinstance(entry, McsTableEntry) or entry.family != "v5":
        raise McsTableError("v5_payload_geometry requires a wire-v5 table entry")
    if type(psdu_bytes) is not int or not (
        V5_PSDU_FCS_BYTES <= psdu_bytes <= V5_MAX_PSDU_BYTES
    ):
        raise McsTableError(
            "psdu_bytes must be an integer in "
            f"[{V5_PSDU_FCS_BYTES}, {V5_MAX_PSDU_BYTES}]"
        )
    return _v5_payload_geometry(entry, psdu_bytes)


@lru_cache(maxsize=64)
def _v5_payload_geometry(entry: McsTableEntry, psdu_bytes: int) -> PayloadGeometry:
    information_bits = 8 * psdu_bytes
    symbols = _v5_symbol_count(entry, information_bits)
    available = V5_DATA_CARRIERS * entry.bits_per_symbol * symbols
    plan = rate_match_plan(information_bits, available, entry.bits_per_symbol)
    return PayloadGeometry(psdu_bytes, information_bits, symbols, available, plan)


def _v5_symbol_count(entry: McsTableEntry, information_bit_count: int) -> int:
    per_symbol = entry.rate.numerator * V5_DATA_CARRIERS * entry.bits_per_symbol
    return max(1, -(-(information_bit_count * entry.rate.denominator) // per_symbol))


def mcs_entry(index: int) -> McsTableEntry:
    """Return the entry addressed by one index, legacy or v5."""

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
    """Return the index naming one configured modulation, scheme and code rate."""

    if not isinstance(modulation, MCS):
        raise McsTableError("modulation must be an MCS")
    rate = _parse_rate(code_rate)
    try:
        return _BY_FORMAT[(modulation, fec_scheme, rate)].index
    except KeyError:
        raise McsTableError(
            f"no table entry for {modulation.name} {fec_scheme} rate {code_rate}"
        ) from None


def _parse_rate(code_rate: str) -> Fraction:
    """Parse ``"1"`` or a reduced ``"a/b"``; any other spelling fails closed."""

    if not isinstance(code_rate, str):
        raise McsTableError(f"unsupported code_rate {code_rate!r}")
    if code_rate == "1":
        return Fraction(1)
    numerator, slash, denominator = code_rate.partition("/")
    if not (slash and numerator.isdigit() and denominator.isdigit()):
        raise McsTableError(f"unsupported code_rate {code_rate!r}")
    if int(denominator) == 0:
        raise McsTableError(f"unsupported code_rate {code_rate!r}")
    rate = Fraction(int(numerator), int(denominator))
    if _rate_label(rate) != code_rate:
        raise McsTableError(f"unsupported code_rate {code_rate!r}")
    return rate


def _rate_label(rate: Fraction) -> str:
    return "1" if rate == 1 else f"{rate.numerator}/{rate.denominator}"


def mcs_entry_for_wire(modulation: MCS, wire_version: int) -> McsTableEntry:
    """Return the legacy entry a received v1-v4 burst header describes."""

    if not isinstance(modulation, MCS):
        raise McsTableError("modulation must be an MCS")
    try:
        return _BY_WIRE[(modulation, wire_version)]
    except KeyError:
        raise McsTableError(
            f"no table entry for {modulation.name} wire version {wire_version}"
        ) from None


def mcs_index_for_wire(modulation: MCS, wire_version: int) -> int:
    """Return the table index a received or configured legacy burst format uses."""

    return mcs_entry_for_wire(modulation, wire_version).index


def describe_mcs_entry(entry: McsTableEntry) -> str:
    """Return one compact human label, ordered like the LTE table columns."""

    scheme = {
        "turbo": "Turbo",
        "turbo_rm": "Turbo",
        "convolutional": "Conv",
        "uncoded": "Uncoded",
    }[entry.fec_scheme]
    modulation = {MCS.QPSK: "QPSK", MCS.QAM16: "16QAM", MCS.QAM64: "64QAM"}[
        entry.modulation
    ]
    return (
        f"MCS {entry.index} — {modulation}, {scheme} r{entry.code_rate} "
        f"({entry.spectral_efficiency:.2f} b/sym)"
    )

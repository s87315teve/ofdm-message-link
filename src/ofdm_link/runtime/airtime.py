"""Exact finite-burst airtime accounting and TDD slot admission."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from ofdm_link.phy import (
    BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    CURRENT_BURST_HEADER_VERSION,
    BurstConfig,
    Frame,
)
from ofdm_link.phy.burst_header import SUPPORTED_WIRE_VERSIONS
from ofdm_link.phy.codec import coded_frame_bit_count_by_wire_version

_NANOSECONDS_PER_SECOND = 1_000_000_000


class AirtimeError(ValueError):
    """Raised when an exact airtime descriptor cannot be represented."""


def burst_sample_count(
    frame: Frame,
    config: BurstConfig | None = None,
    *,
    wire_version: int = CURRENT_BURST_HEADER_VERSION,
) -> int:
    """Return the exact encoded burst length without constructing samples."""

    if not isinstance(frame, Frame):
        raise TypeError("frame must be a Frame")
    if config is None:
        settings = BurstConfig()
    elif isinstance(config, BurstConfig):
        settings = config
    else:
        raise TypeError("config must be a BurstConfig or None")
    if type(wire_version) is not int or wire_version not in SUPPORTED_WIRE_VERSIONS:
        raise AirtimeError(f"unsupported wire_version {wire_version}")

    coded_bits = coded_frame_bit_count_by_wire_version(frame, wire_version)
    if coded_bits > settings.max_coded_payload_bits:
        raise AirtimeError("coded frame exceeds max_coded_payload_bits")

    bits_per_ofdm_symbol = (
        frame.mcs.bits_per_symbol * len(settings.numerology.data_carriers)
    )
    payload_ofdm_symbols = (
        coded_bits + bits_per_ofdm_symbol - 1
    ) // bits_per_ofdm_symbol
    ofdm_symbol_samples = (
        settings.numerology.fft_size + settings.numerology.cp_length
    )
    total = (
        settings.sync.preamble_length
        + ofdm_symbol_samples
        * (
            1
            + BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT
            + payload_ofdm_symbols
        )
    )
    if total > settings.max_burst_samples:
        raise AirtimeError("encoded burst exceeds max_burst_samples")
    return total


class AirtimeDecisionReason(Enum):
    """Reason associated with one immutable admission decision."""

    ADMITTED = "admitted"
    INSUFFICIENT_SLOT_BUDGET = "insufficient_slot_budget"


@dataclass(frozen=True, slots=True)
class AirtimeDecision:
    """Exact candidate timing and remaining budget after one decision."""

    reason: AirtimeDecisionReason
    sample_count: int
    candidate_start_seconds: Fraction
    candidate_end_seconds: Fraction
    duration_seconds: Fraction
    remaining_slot_samples: int
    remaining_slot_seconds: Fraction

    @property
    def admitted(self) -> bool:
        """Whether this candidate consumed slot budget."""

        return self.reason is AirtimeDecisionReason.ADMITTED

    @property
    def deferred(self) -> bool:
        """Whether this candidate left the cursor and budget unchanged."""

        return not self.admitted


class AirtimeAdmission:
    """Admit complete bursts sequentially within one exact slot deadline."""

    __slots__ = (
        "_consumed_samples",
        "_cursor_nanoseconds",
        "_deadline_nanoseconds",
        "_sample_rate",
    )

    def __init__(
        self,
        sample_rate: int,
        *,
        cursor_nanoseconds: int,
        deadline_nanoseconds: int,
    ) -> None:
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if type(cursor_nanoseconds) is not int:
            raise ValueError("cursor_nanoseconds must be an integer")
        if (
            type(deadline_nanoseconds) is not int
            or deadline_nanoseconds < cursor_nanoseconds
        ):
            raise ValueError(
                "deadline_nanoseconds must be an integer at or after the cursor"
            )
        self._sample_rate = sample_rate
        self._cursor_nanoseconds = cursor_nanoseconds
        self._deadline_nanoseconds = deadline_nanoseconds
        self._consumed_samples = 0

    def admit(self, sample_count: int) -> AirtimeDecision:
        """Admit one whole burst, or defer it without advancing the cursor."""

        if type(sample_count) is not int or sample_count <= 0:
            raise ValueError("sample_count must be a positive integer")

        denominator = self._sample_rate * _NANOSECONDS_PER_SECOND
        start_scaled = (
            self._cursor_nanoseconds * self._sample_rate
            + self._consumed_samples * _NANOSECONDS_PER_SECOND
        )
        end_scaled = start_scaled + sample_count * _NANOSECONDS_PER_SECOND
        deadline_scaled = self._deadline_nanoseconds * self._sample_rate
        admitted = end_scaled <= deadline_scaled
        if admitted:
            self._consumed_samples += sample_count
            remaining_scaled = deadline_scaled - end_scaled
            reason = AirtimeDecisionReason.ADMITTED
        else:
            remaining_scaled = deadline_scaled - start_scaled
            reason = AirtimeDecisionReason.INSUFFICIENT_SLOT_BUDGET

        return AirtimeDecision(
            reason=reason,
            sample_count=sample_count,
            candidate_start_seconds=Fraction(start_scaled, denominator),
            candidate_end_seconds=Fraction(end_scaled, denominator),
            duration_seconds=Fraction(sample_count, self._sample_rate),
            remaining_slot_samples=remaining_scaled // _NANOSECONDS_PER_SECOND,
            remaining_slot_seconds=Fraction(remaining_scaled, denominator),
        )

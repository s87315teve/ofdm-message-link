"""Wire-v5 burst: STF/LTF preamble, coded SIG, rate-matched Turbo payload.

Layout in samples from the first STF sample (no guards; the transmitter adds
those)::

    | STF 160 | LTF 160 | SIG 3x80 | payload N_sym x 80 |
    0        160       320        560

The payload is one PSDU (the body plus a 4-byte CRC-32 FCS), Turbo coded,
rate matched to exactly ``48 Qm N_sym`` bits, scrambled with the seed the SIG
carries and mapped carrier by carrier.  Pilots continue from the SIG's
symbol index, so pilot tracking and sample-clock tracking work exactly as on
the legacy payload.

A failed FCS is a *result*, not an exception: the receiver returns the
decoded body with ``fcs_ok=False`` and the per-code-block CRCs, so the
aggregation layer can still deliver every subframe whose own CRC passes.
Exceptions are reserved for bursts whose length or content cannot be trusted
at all (no LTF, SIG failure, truncated input).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray

from .burst import (
    BurstConfig,
    BurstDecodeError,
    BurstDiagnostics,
    BurstValidationError,
    DecodedBurst,
    _apply_sample_clock_tracking,
    _finite_tuple,
    _frozen_samples,
    _require_config,
)
from .channel import (
    EqualizedBatch,
    RefinedChannel,
    equalize_with_pilots,
    refine_channel_estimate,
)
from .codec import MCS, map_symbols
from .header_v5 import (
    V5_HEADER_OFDM_SYMBOL_COUNT,
    V5_MIN_PSDU_BYTES,
    V5Header,
    V5HeaderDecodeError,
    decode_v5_header,
    v5_header_symbols,
)
from .mcs_table import (
    V5_WIRE_VERSION,
    McsTableEntry,
    PayloadGeometry,
    mcs_entry,
    v5_entries,
    v5_payload_geometry,
)
from .ofdm import modulate_ofdm
from .psdu_codec import PsduDecodeResult, RateMatchedTurboCodec
from .scrambler import descramble_llrs, scramble_bits
from .soft import SoftDemapResult, soft_demap_symbols
from .sync_v5 import (
    V5_FIXED_LENGTH,
    V5_SIG_OFFSET,
    V5Acquisition,
    correct_v5_cfo,
    generate_ltf,
    generate_stf,
    measure_ltf_pair,
)

PSDU_FCS_BYTES = 4
_BLOCK_LENGTH = 80
# Header LLRs are clipped like every other LLR before the Viterbi sees them.
_SIG_LLR_CLIP = 64.0


@dataclass(frozen=True, slots=True)
class EncodedBurstV5:
    """One v5 burst waveform without guards, and what it signals."""

    samples: NDArray[np.complex64]
    header: V5Header
    geometry: PayloadGeometry

    def __post_init__(self) -> None:
        samples = _frozen_samples(self.samples, "samples", BurstValidationError)
        if not isinstance(self.header, V5Header):
            raise BurstValidationError("header must be a V5Header")
        expected = V5_FIXED_LENGTH + _BLOCK_LENGTH * self.geometry.ofdm_symbol_count
        if samples.size != expected:
            raise BurstValidationError("v5 burst length does not match its geometry")
        object.__setattr__(self, "samples", samples)

    @property
    def sample_count(self) -> int:
        """Total samples in the serialized burst (no guards)."""

        return int(self.samples.size)


@dataclass(frozen=True, slots=True)
class DecodedPsduBurst:
    """One v5 burst's PSDU body and its exact half-open range in the input.

    ``body`` excludes the FCS.  ``result.fcs_ok`` says whether the whole PSDU
    checked; when it did not, ``result.block_crc_ok`` and
    ``result.block_byte_ranges`` say which parts of ``body`` still came from a
    CRC-valid code block.
    """

    body: bytes
    result: PsduDecodeResult
    mcs_index: int
    modulation: MCS
    burst_start: int
    burst_end: int
    diagnostics: BurstDiagnostics
    payload_symbols: NDArray[np.complex64]

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise BurstDecodeError("body must be bytes")
        if (
            type(self.burst_start) is not int
            or type(self.burst_end) is not int
            or self.burst_start < 0
            or self.burst_end <= self.burst_start
        ):
            raise BurstDecodeError("burst range must be a non-empty half-open range")
        if not isinstance(self.diagnostics, BurstDiagnostics):
            raise BurstDecodeError("diagnostics must be BurstDiagnostics")
        symbols = _frozen_samples(self.payload_symbols, "payload_symbols", BurstDecodeError)
        object.__setattr__(self, "payload_symbols", symbols)

    @property
    def fcs_ok(self) -> bool:
        """Whether the PSDU's CRC-32 FCS checked."""

        return bool(self.result.fcs_ok)

    @property
    def consumed_range(self) -> tuple[int, int]:
        """The half-open ``[start, end)`` range consumed from the input."""

        return (self.burst_start, self.burst_end)


AnyDecodedBurst = DecodedBurst | DecodedPsduBurst


def _require_v5_entry(entry: McsTableEntry) -> McsTableEntry:
    if not isinstance(entry, McsTableEntry) or entry.family != "v5":
        raise BurstValidationError("entry must be a v5 MCS-table entry")
    return entry


def v5_burst_sample_count(entry: McsTableEntry, body_bytes: int) -> int:
    """Samples of a v5 burst carrying ``body_bytes`` (FCS excluded), no guards."""

    _require_v5_entry(entry)
    if type(body_bytes) is not int or body_bytes < 0:
        raise BurstValidationError("body_bytes must be a non-negative integer")
    geometry = v5_payload_geometry(entry, body_bytes + PSDU_FCS_BYTES)
    return V5_FIXED_LENGTH + _BLOCK_LENGTH * geometry.ofdm_symbol_count


@lru_cache(maxsize=4)
def _codec(max_iterations: int) -> RateMatchedTurboCodec:
    return RateMatchedTurboCodec(max_iterations=max_iterations)


@lru_cache(maxsize=1)
def _valid_mcs() -> frozenset[int]:
    return frozenset(entry.index for entry in v5_entries(include_hidden=True))


@lru_cache(maxsize=1)
def _preamble() -> NDArray[np.complex64]:
    preamble = np.concatenate((generate_stf(), generate_ltf())).astype(np.complex64)
    preamble.setflags(write=False)
    return preamble


def encode_burst_v5(
    body: bytes,
    entry: McsTableEntry,
    config: BurstConfig | None = None,
    *,
    scrambler_seed: int,
) -> EncodedBurstV5:
    """Encode one PSDU body (FCS appended here) as a v5 burst.

    ``scrambler_seed`` 1-127 is the normal case: the transmitter rotates it
    per burst.  0 switches the scrambler off (experiment only); the receiver
    needs no setting either way because the seed travels in the SIG.
    """

    settings = _require_config(config)
    _require_v5_entry(entry)
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise BurstValidationError("body must be bytes")
    body = bytes(body)
    psdu_length = len(body) + PSDU_FCS_BYTES
    if not V5_MIN_PSDU_BYTES <= psdu_length <= 0xFFFF:
        raise BurstValidationError(
            f"PSDU length {psdu_length} outside [{V5_MIN_PSDU_BYTES}, 65535]"
        )
    if type(scrambler_seed) is not int or not 0 <= scrambler_seed <= 127:
        raise BurstValidationError("scrambler_seed must be an integer in [0, 127]")
    header = V5Header(
        mcs_index=entry.index,
        psdu_length=psdu_length,
        scrambler_seed=scrambler_seed,
    )
    geometry = v5_payload_geometry(entry, psdu_length)
    coded = _codec(settings.v5.turbo_max_iterations).encode(body, geometry)
    if coded.size != geometry.available_bits:
        raise BurstValidationError("rate matcher did not fill the payload exactly")
    symbols = map_symbols(scramble_bits(coded, scrambler_seed), entry.modulation)
    numerology = settings.numerology
    sig = modulate_ofdm(
        v5_header_symbols(header),
        numerology,
        first_symbol_index=0,
        pilot_version=settings.pilot_version,
    )
    payload = modulate_ofdm(
        symbols,
        numerology,
        first_symbol_index=V5_HEADER_OFDM_SYMBOL_COUNT,
        pilot_version=settings.pilot_version,
    )
    if payload.ofdm_symbol_count != geometry.ofdm_symbol_count or payload.padding_symbol_count:
        raise BurstValidationError("payload does not fill whole OFDM symbols")
    samples = np.concatenate((_preamble(), sig.samples, payload.samples)).astype(
        np.complex64, copy=False
    )
    if samples.size > settings.max_burst_samples:
        raise BurstValidationError("encoded burst exceeds max_burst_samples")
    return EncodedBurstV5(samples=samples, header=header, geometry=geometry)


def _decode_v5_acquired(
    values: NDArray[np.complex64],
    acquisition: V5Acquisition,
    settings: BurstConfig,
    *,
    input_noise_variance: float | None = None,
) -> DecodedPsduBurst:
    """Decode one v5 burst whose preamble is already acquired.

    Rejections mirror the legacy path's messages so the stream counts them
    the same way: "fixed signaling header decode failed: ..." and
    "signaled ..." are header failures, and a ``required_sample_count`` means
    "wait for more samples".
    """

    rx = settings.v5
    numerology = settings.numerology
    stf_start = acquisition.stf_start
    sig_start = stf_start + V5_SIG_OFFSET
    fixed_end = stf_start + V5_FIXED_LENGTH
    if fixed_end > values.size:
        raise BurstDecodeError("truncated burst before the fixed signaling header")
    # Every FFT window -- LTF, SIG, payload -- starts this many samples into
    # its cyclic prefix, so the linear phase it costs is the same everywhere
    # and the LTF channel estimate carries it to the data.
    backoff = rx.fft_backoff_samples

    try:
        ltf = measure_ltf_pair(
            values, acquisition, numerology, average=rx.ltf_average, backoff=backoff
        )
        if ltf is None:
            raise BurstDecodeError(
                "fixed signaling header decode failed: the LTF pair is unusable"
            )
        refined = (
            refine_channel_estimate(
                ltf.least_squares,
                numerology,
                preamble_noise_variance=ltf.noise_variance,
                preamble_degrees_of_freedom=ltf.degrees_of_freedom,
                averaged_symbols=2 if rx.ltf_average else 1,
            )
            if rx.channel_smoothing
            else RefinedChannel(ltf.least_squares, ltf.noise_variance, False)
        )
        channel = refined.estimate
        corrected_sig = correct_v5_cfo(
            values[sig_start - backoff : fixed_end - backoff],
            start=sig_start - backoff,
            acquisition=acquisition,
        )
        equalized_sig = equalize_with_pilots(
            corrected_sig,
            channel,
            numerology,
            first_symbol_index=0,
            pilot_version=settings.pilot_version,
            common_phase_only=True,
        )
        sig_symbols = equalized_sig.data_symbols.reshape(-1)
        enhancement = equalized_sig.noise_enhancement.reshape(-1)
        sig_llrs = np.clip(
            4.0 * sig_symbols.real / (enhancement * refined.noise_variance),
            -_SIG_LLR_CLIP,
            _SIG_LLR_CLIP,
        ).astype(np.float32)
        header = decode_v5_header(
            sig_llrs,
            valid_mcs=_valid_mcs(),
            max_psdu_bytes=rx.max_psdu_bytes,
        )
    except BurstDecodeError:
        raise
    except (V5HeaderDecodeError, ValueError) as error:
        raise BurstDecodeError(f"fixed signaling header decode failed: {error}") from error

    entry = mcs_entry(header.mcs_index)
    geometry = v5_payload_geometry(entry, header.psdu_length)
    payload_sample_count = _BLOCK_LENGTH * geometry.ofdm_symbol_count
    logical_burst_end = fixed_end + payload_sample_count
    if logical_burst_end - stf_start > settings.max_burst_samples:
        raise BurstDecodeError("signaled burst exceeds max_burst_samples")
    timing_margin = int(np.ceil(settings.sample_clock_tracking.max_abs_timing_shift_samples)) + 1
    payload_shortage = max(0, logical_burst_end - values.size)
    if payload_shortage > timing_margin:
        raise BurstDecodeError(
            f"truncated payload: need {logical_burst_end} samples, received {values.size}",
            required_sample_count=logical_burst_end,
        )

    header_noise = _sig_noise_variance(header, equalized_sig)
    noise_variance = {
        "max": max(header_noise, refined.noise_variance),
        "header": header_noise,
        "ltf": refined.noise_variance,
    }[rx.llr_noise_source]
    if input_noise_variance is not None:
        noise_variance = float(input_noise_variance)

    payload_source_end = min(values.size, logical_burst_end + timing_margin)
    corrected_source = correct_v5_cfo(
        values[fixed_end - backoff : payload_source_end],
        start=fixed_end - backoff,
        acquisition=acquisition,
    )
    corrected_payload = corrected_source[:payload_sample_count]
    if corrected_payload.size < payload_sample_count:
        corrected_payload = np.pad(
            corrected_payload, (0, payload_sample_count - corrected_payload.size)
        )
    magnitude_correction = entry.modulation is not MCS.QPSK
    try:
        # Slopes are unwrapped along time, so a drift past 2.3 samples -- where
        # pilots 14 carriers apart alias -- still reads as what it is.
        provisional = equalize_with_pilots(
            corrected_payload,
            channel,
            numerology,
            first_symbol_index=V5_HEADER_OFDM_SYMBOL_COUNT,
            pilot_version=settings.pilot_version,
            correct_pilot_magnitude=magnitude_correction,
            track_slope_in_time=True,
        )
        corrected_payload, tracking = _apply_sample_clock_tracking(
            corrected_sig,
            corrected_source,
            payload_start_from_preamble=V5_FIXED_LENGTH,
            payload_sample_count=payload_sample_count,
            mcs=entry.modulation,
            phase_slope_rad_per_carrier=provisional.phase_slope_rad_per_carrier,
            config=settings,
            realign_windows=True,
        )
        if payload_shortage and (
            not tracking.correction_applied or tracking.applied_correction_ppm >= 0.0
        ):
            raise BurstDecodeError(
                f"truncated payload: need {logical_burst_end} samples, received {values.size}",
                required_sample_count=logical_burst_end,
            )
        equalized_payload = (
            provisional
            if tracking.applied_correction_ppm == 0.0
            else equalize_with_pilots(
                corrected_payload,
                channel,
                numerology,
                first_symbol_index=V5_HEADER_OFDM_SYMBOL_COUNT,
                pilot_version=settings.pilot_version,
                correct_pilot_magnitude=magnitude_correction,
                track_slope_in_time=True,
            )
        )
        payload_symbols = equalized_payload.data_symbols.reshape(-1)
        soft: SoftDemapResult = soft_demap_symbols(
            payload_symbols,
            entry.modulation,
            equalized_payload.post_equalization_noise_variance(noise_variance).reshape(-1),
        )
        llrs = descramble_llrs(soft.llrs, header.scrambler_seed)
    except BurstDecodeError:
        raise
    except ValueError as error:
        raise BurstDecodeError(f"payload frame decode failed: {error}") from error

    decoder_started = time.perf_counter()
    try:
        result = _codec(rx.turbo_max_iterations).decode(llrs, geometry)
    except (TypeError, ValueError, RuntimeError) as error:
        raise BurstDecodeError(f"payload frame decode failed: {error}") from error
    decoder_latency = time.perf_counter() - decoder_started

    last_used = logical_burst_end - stf_start - 1 + tracking.timing_correction_end_samples
    observed_end = stf_start + int(np.ceil(last_used)) + 1
    report = result.report
    diagnostics = BurstDiagnostics(
        acquisition=acquisition,
        channel_estimate=channel,
        header_common_phase_rad=_finite_tuple(equalized_sig.common_phase_rad),
        header_phase_slope_rad_per_carrier=_finite_tuple(
            equalized_sig.phase_slope_rad_per_carrier
        ),
        payload_common_phase_rad=_finite_tuple(equalized_payload.common_phase_rad),
        payload_phase_slope_rad_per_carrier=_finite_tuple(
            equalized_payload.phase_slope_rad_per_carrier
        ),
        sample_clock_tracking=tracking,
        payload_pilot_magnitude=equalized_payload.magnitude_diagnostics,
        wire_version=V5_WIRE_VERSION,
        active_fec_profile=f"lte-turbo-rm r{entry.rate}",
        decision_mode="turbo",
        code_block_count=report.block_count,
        filler_bit_count=report.filler_bit_count,
        configured_max_iterations=report.configured_max_iterations,
        actual_iterations_per_block=tuple(report.actual_iterations_per_block),
        early_stop_reasons=tuple(report.early_stop_reasons),
        code_block_crc_ok=tuple(report.code_block_crc_ok),
        outer_crc_ok=bool(result.fcs_ok),
        estimated_noise_variance=soft.noise_variance_mean,
        llr_saturation_count=soft.saturation_count,
        llr_saturation_rate=soft.saturation_rate,
        decoder_latency_seconds=decoder_latency,
        decode_outcome="decoded" if result.fcs_ok else "payload_decode_failed",
    )
    return DecodedPsduBurst(
        body=bytes(result.body),
        result=result,
        mcs_index=entry.index,
        modulation=entry.modulation,
        burst_start=stf_start,
        burst_end=observed_end,
        diagnostics=diagnostics,
        payload_symbols=payload_symbols,
    )


def _sig_noise_variance(header: V5Header, equalized_sig: EqualizedBatch) -> float:
    """Pre-equalization noise variance measured on the decoded SIG.

    Same rule as the legacy header (improve-rx): once the SIG decodes, its 144
    BPSK symbols are known and went through the same channel estimate and
    equalizer the payload will, so their error includes what the LTF
    difference cannot see -- channel-estimation error, TX distortion and IQ
    imbalance, which are identical in L1 and L2 and cancel there.
    """

    reference = v5_header_symbols(header).reshape(equalized_sig.data_symbols.shape)
    error = np.abs(equalized_sig.data_symbols - reference) ** 2
    return float(max(np.mean(error / equalized_sig.noise_enhancement), 1e-12))

"""Finite OFDM burst composition and acquisition reference path.

The module joins the versioned PHY building blocks without bypassing any wire
processing.  It is a vectorized NumPy reference for interoperability tests;
stream scheduling and radio I/O belong to the GNU Radio runtime.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite
from .burst_header import (
    BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    CURRENT_BURST_HEADER_VERSION,
    SUPPORTED_WIRE_VERSIONS,
    TURBO_BURST_HEADER_VERSION,
    BurstHeader,
    BurstHeaderDescriptor,
    BurstHeaderIntegrityError,
    TurboBurstHeader,
    decode_burst_header,
    decode_burst_header_soft,
    encode_burst_header,
)
from .channel import (
    ChannelEstimate,
    EqualizedBatch,
    PilotMagnitudeDiagnostics,
    conjugate_rotation,
    equalize_with_pilots,
    estimate_channel,
    generate_training_symbol,
)
from .codec import (
    DEFAULT_DUAL_VERSION_FRAME_DECODER,
    MAX_CODED_FRAME_BITS,
    MAX_TURBO_CODED_FRAME_BITS,
    MCS,
    Frame,
    FrameDecodeFailure,
    FrameDecodeResult,
    FrameIntegrityError,
    coded_frame_bit_count_by_wire_version,
    demap_symbols,
    encode_frame_by_wire_version,
    frame_codec_for_wire_version,
    information_frame_bit_count,
    map_symbols,
)
from .ofdm import OfdmNumerology, modulate_ofdm
from .soft import SoftDemapResult, soft_demap_symbols
from .sync import AcquisitionResult, SyncConfig, acquire_preamble, generate_preamble
from .turbo import TurboDecodeReport


class BurstError(ValueError):
    """Base class for rejected burst operations."""


class BurstValidationError(BurstError):
    """Raised when an encoder input or burst configuration is invalid."""


class BurstDecodeError(BurstError):
    """Raised when a finite sample buffer does not contain one valid burst."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: BurstDiagnostics | None = None,
        required_sample_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
        self.required_sample_count = required_sample_count


FrameEncoder = Callable[[Frame], NDArray[np.uint8]]
FrameDecoder = Callable[..., Frame]
SymbolMapper = Callable[[NDArray[np.uint8], MCS], ArrayLike]
SymbolDemapper = Callable[[NDArray[np.complex64], MCS], ArrayLike]
SoftSymbolDemapper = Callable[
    [NDArray[np.complex64], MCS, ArrayLike],
    SoftDemapResult,
]


@dataclass(frozen=True, slots=True)
class SampleClockTrackingConfig:
    """Bounded pilot-driven timing-correction controls for one burst."""

    enabled: bool = True
    loop_gain: float = 1.0
    max_abs_correction_ppm: float = 40.0
    max_timing_slew_samples_per_symbol: float = 0.004
    max_abs_timing_shift_samples: float = 4.0
    minimum_normalized_timing_shift: float = 0.45
    minimum_payload_symbols: int = 8

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise BurstValidationError("sample-clock tracking enabled must be a bool")
        positive_values = (
            ("loop_gain", self.loop_gain),
            ("max_abs_correction_ppm", self.max_abs_correction_ppm),
            (
                "max_timing_slew_samples_per_symbol",
                self.max_timing_slew_samples_per_symbol,
            ),
            ("max_abs_timing_shift_samples", self.max_abs_timing_shift_samples),
        )
        for name, value in positive_values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BurstValidationError(f"{name} must be a positive finite number")
            if not np.isfinite(value) or value <= 0:
                raise BurstValidationError(f"{name} must be a positive finite number")
        if (
            isinstance(self.minimum_normalized_timing_shift, bool)
            or not isinstance(self.minimum_normalized_timing_shift, (int, float))
            or not np.isfinite(self.minimum_normalized_timing_shift)
            or self.minimum_normalized_timing_shift < 0
        ):
            raise BurstValidationError(
                "minimum_normalized_timing_shift must be a non-negative finite number"
            )
        integer_values = (
            ("minimum_payload_symbols", self.minimum_payload_symbols, 2),
        )
        for name, value, minimum in integer_values:
            if type(value) is not int or value < minimum:
                raise BurstValidationError(f"{name} must be an integer at least {minimum}")


@dataclass(frozen=True, slots=True)
class BurstConfig:
    """Validated resource and allocation limits for one finite burst."""

    numerology: OfdmNumerology = field(default_factory=OfdmNumerology)
    sync: SyncConfig = field(default_factory=SyncConfig)
    sample_clock_tracking: SampleClockTrackingConfig = field(
        default_factory=SampleClockTrackingConfig
    )
    pilot_version: int = 1
    max_coded_payload_bits: int = MAX_CODED_FRAME_BITS
    max_burst_samples: int = 900_000
    max_input_samples: int = 2_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.numerology, OfdmNumerology):
            raise BurstValidationError("numerology must be an OfdmNumerology")
        if not isinstance(self.sync, SyncConfig):
            raise BurstValidationError("sync must be a SyncConfig")
        if not isinstance(self.sample_clock_tracking, SampleClockTrackingConfig):
            raise BurstValidationError(
                "sample_clock_tracking must be a SampleClockTrackingConfig"
            )
        if (
            self.sync.fft_size != self.numerology.fft_size
            or self.sync.cyclic_prefix_length != self.numerology.cp_length
        ):
            raise BurstValidationError("sync FFT/CP must match the OFDM numerology")
        if self.pilot_version != 1:
            raise BurstValidationError("pilot_version must be the supported version 1")
        if (
            type(self.max_coded_payload_bits) is not int
            or not 1 <= self.max_coded_payload_bits <= MAX_TURBO_CODED_FRAME_BITS
        ):
            raise BurstValidationError(
                "max_coded_payload_bits must be an integer in "
                f"[1, {MAX_TURBO_CODED_FRAME_BITS}]"
            )
        if type(self.max_burst_samples) is not int or self.max_burst_samples <= 0:
            raise BurstValidationError("max_burst_samples must be a positive integer")
        if (
            type(self.max_input_samples) is not int
            or self.max_input_samples < self.max_burst_samples
        ):
            raise BurstValidationError(
                "max_input_samples must be an integer at least max_burst_samples"
            )


@dataclass(frozen=True, slots=True)
class EncodedBurst:
    """Immutable waveform and exact serialized section sizes."""

    samples: NDArray[np.complex64]
    header: BurstHeaderDescriptor
    preamble_sample_count: int
    training_sample_count: int
    header_sample_count: int
    payload_sample_count: int

    def __post_init__(self) -> None:
        samples = _frozen_samples(self.samples, "samples", BurstValidationError)
        if not isinstance(self.header, (BurstHeader, TurboBurstHeader)):
            raise BurstValidationError("header must be a versioned burst header")
        counts = (
            self.preamble_sample_count,
            self.training_sample_count,
            self.header_sample_count,
            self.payload_sample_count,
        )
        if any(type(count) is not int or count <= 0 for count in counts):
            raise BurstValidationError("burst section sample counts must be positive integers")
        if samples.size != sum(counts):
            raise BurstValidationError("burst section sample counts do not match samples")
        object.__setattr__(self, "samples", samples)

    @property
    def sample_count(self) -> int:
        """Total samples in the serialized burst."""

        return int(self.samples.size)


@dataclass(frozen=True, slots=True)
class SampleClockTrackingDiagnostics:
    """Constant-size timing-loop observations for one payload."""

    symbols_observed: int
    observed_slope_rate_rad_per_carrier_per_symbol: float
    estimated_sfo_ppm: float
    applied_correction_ppm: float
    resampling_ratio: float
    timing_correction_start_samples: float
    timing_correction_end_samples: float
    max_abs_timing_correction_samples: float
    normalized_timing_risk: float
    correction_applied: bool
    limit_event_count: int
    limit_hit: bool

    def __post_init__(self) -> None:
        if type(self.symbols_observed) is not int or self.symbols_observed <= 0:
            raise BurstDecodeError("symbols_observed must be a positive integer")
        finite_values = (
            self.observed_slope_rate_rad_per_carrier_per_symbol,
            self.estimated_sfo_ppm,
            self.applied_correction_ppm,
            self.resampling_ratio,
            self.timing_correction_start_samples,
            self.timing_correction_end_samples,
            self.max_abs_timing_correction_samples,
            self.normalized_timing_risk,
        )
        if not np.all(np.isfinite(finite_values)):
            raise BurstDecodeError("sample-clock diagnostics must be finite")
        if self.resampling_ratio <= 0:
            raise BurstDecodeError("resampling_ratio must be positive")
        if self.max_abs_timing_correction_samples < 0:
            raise BurstDecodeError("max timing correction must be non-negative")
        if self.normalized_timing_risk < 0:
            raise BurstDecodeError("normalized timing risk must be non-negative")
        if type(self.correction_applied) is not bool:
            raise BurstDecodeError("correction_applied must be a bool")
        if type(self.limit_event_count) is not int or self.limit_event_count < 0:
            raise BurstDecodeError("limit_event_count must be a non-negative integer")
        if type(self.limit_hit) is not bool:
            raise BurstDecodeError("limit_hit must be a bool")
        if self.limit_hit != (self.limit_event_count > 0):
            raise BurstDecodeError("limit_hit must match limit_event_count")


@dataclass(frozen=True, slots=True)
class BurstDiagnostics:
    """Synchronization, channel, and per-symbol phase observations."""

    acquisition: AcquisitionResult
    channel_estimate: ChannelEstimate
    header_common_phase_rad: tuple[float, ...]
    header_phase_slope_rad_per_carrier: tuple[float, ...]
    payload_common_phase_rad: tuple[float, ...]
    payload_phase_slope_rad_per_carrier: tuple[float, ...]
    sample_clock_tracking: SampleClockTrackingDiagnostics
    payload_pilot_magnitude: PilotMagnitudeDiagnostics
    wire_version: int
    active_fec_profile: str
    decision_mode: str
    code_block_count: int
    filler_bit_count: int
    configured_max_iterations: int
    actual_iterations_per_block: tuple[int, ...]
    early_stop_reasons: tuple[str, ...]
    code_block_crc_ok: tuple[bool | None, ...]
    outer_crc_ok: bool | None
    estimated_noise_variance: float | None
    llr_saturation_count: int
    llr_saturation_rate: float
    decoder_latency_seconds: float
    decode_outcome: str

    def __post_init__(self) -> None:
        if not isinstance(self.acquisition, AcquisitionResult):
            raise BurstDecodeError("acquisition must be an AcquisitionResult")
        if not isinstance(self.channel_estimate, ChannelEstimate):
            raise BurstDecodeError("channel_estimate must be a ChannelEstimate")
        if not isinstance(self.sample_clock_tracking, SampleClockTrackingDiagnostics):
            raise BurstDecodeError(
                "sample_clock_tracking must be SampleClockTrackingDiagnostics"
            )
        if not isinstance(self.payload_pilot_magnitude, PilotMagnitudeDiagnostics):
            raise BurstDecodeError(
                "payload_pilot_magnitude must be PilotMagnitudeDiagnostics"
            )
        if self.decode_outcome not in {"decoded", "payload_decode_failed"}:
            raise BurstDecodeError("decode_outcome must describe the payload result")
        if self.wire_version not in SUPPORTED_WIRE_VERSIONS:
            raise BurstDecodeError(
                "wire_version must identify a defined burst wire version"
            )
        if not isinstance(self.active_fec_profile, str) or not self.active_fec_profile:
            raise BurstDecodeError("active_fec_profile must be non-empty")
        if self.decision_mode not in {"hard", "soft", "turbo"}:
            raise BurstDecodeError("decision_mode must be 'hard', 'soft', or 'turbo'")
        if type(self.code_block_count) is not int or self.code_block_count < 1:
            raise BurstDecodeError("code_block_count must be positive")
        if type(self.filler_bit_count) is not int or self.filler_bit_count < 0:
            raise BurstDecodeError("filler_bit_count must be non-negative")
        if (
            type(self.configured_max_iterations) is not int
            or self.configured_max_iterations < 0
        ):
            raise BurstDecodeError("configured_max_iterations must be non-negative")
        if self.estimated_noise_variance is not None and (
            not np.isfinite(self.estimated_noise_variance)
            or self.estimated_noise_variance <= 0.0
        ):
            raise BurstDecodeError("estimated_noise_variance must be positive and finite")
        if (
            type(self.llr_saturation_count) is not int
            or self.llr_saturation_count < 0
        ):
            raise BurstDecodeError("llr_saturation_count must be non-negative")
        if (
            not np.isfinite(self.llr_saturation_rate)
            or not 0.0 <= self.llr_saturation_rate <= 1.0
        ):
            raise BurstDecodeError("llr_saturation_rate must be finite and in [0, 1]")
        if (
            not np.isfinite(self.decoder_latency_seconds)
            or self.decoder_latency_seconds < 0.0
        ):
            raise BurstDecodeError("decoder_latency_seconds must be finite and non-negative")
        if self.decision_mode == "hard" and (
            self.estimated_noise_variance is not None
            or self.llr_saturation_count
            or self.llr_saturation_rate
        ):
            raise BurstDecodeError("hard decision diagnostics cannot report LLR metrics")
        pairs = (
            (self.header_common_phase_rad, self.header_phase_slope_rad_per_carrier, 3),
            (self.payload_common_phase_rad, self.payload_phase_slope_rad_per_carrier, None),
        )
        for common, slope, expected in pairs:
            if not isinstance(common, tuple) or not isinstance(slope, tuple):
                raise BurstDecodeError("phase diagnostics must be immutable tuples")
            if len(common) != len(slope) or not common:
                raise BurstDecodeError("phase diagnostics must have matching non-empty lengths")
            if expected is not None and len(common) != expected:
                raise BurstDecodeError(f"header phase diagnostics must contain {expected} symbols")
            if not np.all(np.isfinite(common)) or not np.all(np.isfinite(slope)):
                raise BurstDecodeError("phase diagnostics must contain only finite values")


@dataclass(frozen=True, slots=True)
class DecodedBurst:
    """One valid frame and its exact half-open range in the input buffer."""

    frame: Frame
    burst_start: int
    burst_end: int
    diagnostics: BurstDiagnostics
    payload_symbols: NDArray[np.complex64]

    def __post_init__(self) -> None:
        if not isinstance(self.frame, Frame):
            raise BurstDecodeError("frame must be a Frame")
        if (
            type(self.burst_start) is not int
            or type(self.burst_end) is not int
            or self.burst_start < 0
            or self.burst_end <= self.burst_start
        ):
            raise BurstDecodeError("burst range must be a non-empty half-open range")
        if not isinstance(self.diagnostics, BurstDiagnostics):
            raise BurstDecodeError("diagnostics must be BurstDiagnostics")
        symbols = _frozen_samples(
            self.payload_symbols,
            "payload_symbols",
            BurstDecodeError,
        )
        if symbols.size == 0:
            raise BurstDecodeError("payload_symbols must not be empty")
        object.__setattr__(self, "payload_symbols", symbols)

    @property
    def consumed_range(self) -> tuple[int, int]:
        """The half-open ``[start, end)`` range consumed from the input."""

        return (self.burst_start, self.burst_end)


def encode_burst(
    frame: Frame,
    config: BurstConfig | None = None,
    *,
    symbol_mapper: SymbolMapper = map_symbols,
    wire_version: int = CURRENT_BURST_HEADER_VERSION,
    frame_encoder: FrameEncoder | None = None,
    preencoded_frame_bits: ArrayLike | None = None,
) -> EncodedBurst:
    """Encode one frame as preamble, training, fixed-QPSK header, and payload.

    ``preencoded_frame_bits`` is an optimization seam for a caller that already
    produced and validated ``encode_frame(frame)`` at its trust boundary.
    This function still validates shape, binary values, and exact wire length,
    but deliberately does not repeat the convolutional encoding operation.
    """

    settings = _require_config(config)
    if not callable(symbol_mapper):
        raise BurstValidationError("symbol_mapper must be callable")
    if not isinstance(frame, Frame):
        raise BurstValidationError("frame must be a Frame")
    if wire_version not in SUPPORTED_WIRE_VERSIONS:
        raise BurstValidationError(f"unsupported wire_version {wire_version}")
    if frame_encoder is not None and not callable(frame_encoder):
        raise BurstValidationError("frame_encoder must be callable or None")

    if preencoded_frame_bits is None:
        selected_encoder = frame_encoder or (
            lambda candidate: encode_frame_by_wire_version(candidate, wire_version)
        )
        coded_payload = _preencoded_frame_bits(
            frame,
            selected_encoder(frame),
            wire_version,
        )
    else:
        coded_payload = _preencoded_frame_bits(
            frame,
            preencoded_frame_bits,
            wire_version,
        )
    if coded_payload.size > settings.max_coded_payload_bits:
        raise BurstValidationError("coded frame exceeds max_coded_payload_bits")

    header: BurstHeaderDescriptor = (
        TurboBurstHeader(
            wire_version,
            frame.mcs,
            information_frame_bit_count(frame),
        )
        if wire_version == TURBO_BURST_HEADER_VERSION
        else BurstHeader(wire_version, frame.mcs, int(coded_payload.size))
    )
    header_symbols = _map_with_backend(
        encode_burst_header(header),
        MCS.QPSK,
        symbol_mapper,
    )
    header_waveform = modulate_ofdm(
        header_symbols,
        settings.numerology,
        first_symbol_index=0,
        pilot_version=settings.pilot_version,
    )
    if header_waveform.ofdm_symbol_count != BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT:
        raise BurstValidationError("burst header does not occupy exactly three OFDM symbols")

    payload_padding = (-coded_payload.size) % frame.mcs.bits_per_symbol
    padded_payload = np.pad(coded_payload, (0, payload_padding))
    payload_symbols = _map_with_backend(padded_payload, frame.mcs, symbol_mapper)
    payload_waveform = modulate_ofdm(
        payload_symbols,
        settings.numerology,
        first_symbol_index=BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
        pilot_version=settings.pilot_version,
    )
    preamble = generate_preamble(settings.sync)
    training = generate_training_symbol(settings.numerology).samples
    samples = np.concatenate(
        (preamble, training, header_waveform.samples, payload_waveform.samples)
    ).astype(np.complex64, copy=False)
    if samples.size > settings.max_burst_samples:
        raise BurstValidationError("encoded burst exceeds max_burst_samples")

    return EncodedBurst(
        samples=samples,
        header=header,
        preamble_sample_count=preamble.size,
        training_sample_count=training.size,
        header_sample_count=header_waveform.samples.size,
        payload_sample_count=payload_waveform.samples.size,
    )


def _preencoded_frame_bits(
    frame: Frame,
    bits: ArrayLike,
    wire_version: int,
) -> NDArray[np.uint8]:
    values = np.asarray(bits)
    if values.ndim != 1:
        raise BurstValidationError("preencoded_frame_bits must be one-dimensional")
    if not (
        np.issubdtype(values.dtype, np.integer)
        or np.issubdtype(values.dtype, np.bool_)
    ):
        raise BurstValidationError("preencoded_frame_bits must contain integer bits")
    if np.any((values != 0) & (values != 1)):
        raise BurstValidationError("preencoded_frame_bits must contain only 0 and 1")
    expected_size = coded_frame_bit_count_by_wire_version(frame, wire_version)
    if values.size != expected_size:
        raise BurstValidationError(
            "preencoded_frame_bits length does not match the supplied frame"
        )
    return values.astype(np.uint8, copy=False)


def decode_burst(
    samples: ArrayLike,
    config: BurstConfig | None = None,
    *,
    frame_decoder: FrameDecoder = DEFAULT_DUAL_VERSION_FRAME_DECODER,
    symbol_demapper: SymbolDemapper = demap_symbols,
    soft_symbol_demapper: SoftSymbolDemapper | None = None,
    input_noise_variance: float | None = None,
) -> DecodedBurst:
    """Acquire and decode exactly one unambiguous burst in a finite buffer."""

    settings = _require_config(config)
    values = _validated_decode_input(
        samples,
        settings,
        frame_decoder,
        symbol_demapper,
        soft_symbol_demapper,
        input_noise_variance,
    )
    try:
        acquisition = acquire_preamble(values, settings.sync)
    except (TypeError, ValueError) as error:
        raise BurstDecodeError(f"preamble acquisition failed: {error}") from error
    if acquisition is None:
        raise BurstDecodeError("no unambiguous preamble acquired")
    return _decode_acquired_burst(
        values,
        acquisition,
        settings,
        frame_decoder=frame_decoder,
        symbol_demapper=symbol_demapper,
        soft_symbol_demapper=soft_symbol_demapper,
        input_noise_variance=input_noise_variance,
    )


def _decode_burst_with_acquisition(
    samples: ArrayLike,
    config: BurstConfig | None,
    acquisition: AcquisitionResult,
    *,
    frame_decoder: FrameDecoder,
    symbol_demapper: SymbolDemapper,
    soft_symbol_demapper: SoftSymbolDemapper | None,
    input_noise_variance: float | None,
) -> DecodedBurst:
    """Decode one burst whose preamble the caller has already acquired.

    The continuous decoder has to scan for preambles before it can bound a
    burst's samples at all.  Handing that result over instead of discarding it
    saves a second full-buffer acquisition scan for every burst, which is the
    single largest non-FEC cost in the receive path.  The caller owns the
    separation rule it applied; this entry point applies none of its own.
    """

    settings = _require_config(config)
    if not isinstance(acquisition, AcquisitionResult):
        raise BurstValidationError("acquisition must be an AcquisitionResult")
    values = _validated_decode_input(
        samples,
        settings,
        frame_decoder,
        symbol_demapper,
        soft_symbol_demapper,
        input_noise_variance,
    )
    return _decode_acquired_burst(
        values,
        acquisition,
        settings,
        frame_decoder=frame_decoder,
        symbol_demapper=symbol_demapper,
        soft_symbol_demapper=soft_symbol_demapper,
        input_noise_variance=input_noise_variance,
    )


def _validated_decode_input(
    samples: ArrayLike,
    settings: BurstConfig,
    frame_decoder: FrameDecoder,
    symbol_demapper: SymbolDemapper,
    soft_symbol_demapper: SoftSymbolDemapper | None,
    input_noise_variance: float | None,
) -> NDArray[np.complex64]:
    if not callable(frame_decoder):
        raise BurstValidationError("frame_decoder must be callable")
    if not callable(symbol_demapper):
        raise BurstValidationError("symbol_demapper must be callable")
    if soft_symbol_demapper is not None and not callable(soft_symbol_demapper):
        raise BurstValidationError("soft_symbol_demapper must be callable or None")
    if soft_symbol_demapper is not None and (
        isinstance(input_noise_variance, bool)
        or not isinstance(input_noise_variance, (int, float))
        or not np.isfinite(input_noise_variance)
        or input_noise_variance <= 0.0
    ):
        raise BurstValidationError(
            "soft decoding requires a positive finite input_noise_variance"
        )
    return _input_samples(samples, settings)


def _decode_acquired_burst(
    values: NDArray[np.complex64],
    acquisition: AcquisitionResult,
    settings: BurstConfig,
    *,
    frame_decoder: FrameDecoder,
    symbol_demapper: SymbolDemapper,
    soft_symbol_demapper: SoftSymbolDemapper | None,
    input_noise_variance: float | None,
) -> DecodedBurst:
    block_length = settings.numerology.fft_size + settings.numerology.cp_length
    training_start = acquisition.preamble_start + settings.sync.preamble_length
    header_start = training_start + block_length
    header_sample_count = BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT * block_length
    fixed_end = header_start + header_sample_count
    if fixed_end > values.size:
        raise BurstDecodeError("truncated burst before the fixed signaling header")

    corrected_fixed = _correct_cfo(
        values[training_start:fixed_end],
        start_index=training_start - acquisition.preamble_start,
        normalized_cfo=acquisition.normalized_cfo,
        fft_size=settings.numerology.fft_size,
    )
    training_samples = corrected_fixed[:block_length]
    header_samples = corrected_fixed[block_length:]
    try:
        channel = estimate_channel(training_samples, settings.numerology)
        equalized_header = equalize_with_pilots(
            header_samples,
            channel,
            settings.numerology,
            first_symbol_index=0,
            pilot_version=settings.pilot_version,
        )
        header_symbols = equalized_header.data_symbols.reshape(-1)
        header_bits = _demap_with_backend(header_symbols, MCS.QPSK, symbol_demapper)
        try:
            header = decode_burst_header(header_bits)
        except BurstHeaderIntegrityError:
            header = decode_burst_header_soft(_header_llrs(header_symbols))
    except ValueError as error:
        raise BurstDecodeError(f"fixed signaling header decode failed: {error}") from error

    _validate_coded_payload_size(header, settings)
    payload_sample_count = header.payload_ofdm_symbol_count * block_length
    logical_burst_end = fixed_end + payload_sample_count
    burst_sample_count = logical_burst_end - acquisition.preamble_start
    if burst_sample_count > settings.max_burst_samples:
        raise BurstDecodeError("signaled burst exceeds max_burst_samples")
    timing_margin = int(
        np.ceil(settings.sample_clock_tracking.max_abs_timing_shift_samples)
    ) + 1
    payload_shortage = max(0, logical_burst_end - values.size)
    if payload_shortage > timing_margin:
        raise BurstDecodeError(
            f"truncated payload: need {logical_burst_end} samples, received {values.size}",
            required_sample_count=logical_burst_end,
        )

    payload_source_end = min(values.size, logical_burst_end + timing_margin)
    corrected_payload_source = _correct_cfo(
        values[fixed_end:payload_source_end],
        start_index=fixed_end - acquisition.preamble_start,
        normalized_cfo=acquisition.normalized_cfo,
        fft_size=settings.numerology.fft_size,
    )
    corrected_payload = corrected_payload_source[:payload_sample_count]
    if corrected_payload.size < payload_sample_count:
        corrected_payload = np.pad(
            corrected_payload,
            (0, payload_sample_count - corrected_payload.size),
        )
    try:
        provisional_payload = equalize_with_pilots(
            corrected_payload,
            channel,
            settings.numerology,
            first_symbol_index=BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
            pilot_version=settings.pilot_version,
            correct_pilot_magnitude=header.mcs is MCS.QAM16,
        )
        corrected_payload, tracking = _apply_sample_clock_tracking(
            corrected_fixed,
            corrected_payload_source,
            payload_start_from_preamble=fixed_end - acquisition.preamble_start,
            payload_sample_count=payload_sample_count,
            mcs=header.mcs,
            phase_slope_rad_per_carrier=provisional_payload.phase_slope_rad_per_carrier,
            config=settings,
        )
        if payload_shortage and (
            not tracking.correction_applied or tracking.applied_correction_ppm >= 0.0
        ):
            # Sample-clock tracking did not shrink the burst, so the missing
            # samples are genuinely missing.  A continuous receiver can only
            # wait for them when the shortage carries the signaled length.
            raise BurstDecodeError(
                f"truncated payload: need {logical_burst_end} samples, "
                f"received {values.size}",
                required_sample_count=logical_burst_end,
            )
        equalized_payload = (
            provisional_payload
            if tracking.applied_correction_ppm == 0.0
            else equalize_with_pilots(
                corrected_payload,
                channel,
                settings.numerology,
                first_symbol_index=BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
                pilot_version=settings.pilot_version,
                correct_pilot_magnitude=header.mcs is MCS.QAM16,
            )
        )
        payload_symbols = equalized_payload.data_symbols.reshape(-1)[: header.payload_symbol_count]
        turbo_mode = isinstance(header, TurboBurstHeader)
        selected_soft_demapper = (
            soft_symbol_demapper
            if soft_symbol_demapper is not None
            else (soft_demap_symbols if turbo_mode else None)
        )
        soft_result: SoftDemapResult | None = None
        if selected_soft_demapper is None:
            padded_observations = _demap_with_backend(
                payload_symbols,
                header.mcs,
                symbol_demapper,
            )
        else:
            noise_variance = (
                1e-6 if input_noise_variance is None else float(input_noise_variance)
            )
            post_equalization_variance = equalized_payload.post_equalization_noise_variance(
                noise_variance
            ).reshape(-1)[: header.payload_symbol_count]
            soft_result = _soft_demap_with_backend(
                payload_symbols,
                header.mcs,
                post_equalization_variance,
                selected_soft_demapper,
            )
            padded_observations = soft_result.llrs
        coded_payload = padded_observations[: header.coded_payload_bit_count]
    except BurstDecodeError:
        # A rejection raised in here already describes itself, including how
        # many samples it still needs.  Rewrapping it would drop that.
        raise
    except ValueError as error:
        raise BurstDecodeError(f"payload frame decode failed: {error}") from error
    decoder_started = time.perf_counter()
    frame_result: FrameDecodeResult | None = None
    try:
        result_decoder = getattr(frame_decoder, "decode_result", None)
        if callable(result_decoder):
            frame_result = (
                result_decoder(coded_payload)
                if _is_legacy_v1(header)
                else result_decoder(coded_payload, **_decoder_kwargs(header))
            )
            if not isinstance(frame_result, FrameDecodeResult):
                raise TypeError("frame decoder decode_result must return FrameDecodeResult")
            frame = frame_result.frame
        else:
            frame = (
                frame_decoder(coded_payload)
                if _is_legacy_v1(header)
                else frame_decoder(coded_payload, **_decoder_kwargs(header))
            )
    except (TypeError, ValueError) as error:
        decoder_latency = time.perf_counter() - decoder_started
        failure = getattr(error, "failure", None)
        turbo_report = (
            failure.fec_report
            if isinstance(failure, FrameDecodeFailure)
            else None
        )
        diagnostics = _burst_diagnostics(
            acquisition,
            channel,
            equalized_header,
            equalized_payload,
            tracking,
            header=header,
            soft_result=soft_result,
            turbo_report=turbo_report,
            outer_crc_ok=(False if isinstance(error, FrameIntegrityError) else None),
            decoder_latency_seconds=decoder_latency,
            decode_outcome="payload_decode_failed",
        )
        raise BurstDecodeError(
            f"payload frame decode failed: {error}",
            diagnostics=diagnostics,
        ) from error
    decoder_latency = time.perf_counter() - decoder_started
    if not isinstance(frame, Frame):
        raise BurstDecodeError("frame_decoder must return a Frame")
    if frame.mcs is not header.mcs:
        raise BurstDecodeError("burst header and decoded frame MCS do not match")

    last_used_from_preamble = (
        logical_burst_end
        - acquisition.preamble_start
        - 1
        + tracking.timing_correction_end_samples
    )
    observed_burst_end = acquisition.preamble_start + int(
        np.ceil(last_used_from_preamble)
    ) + 1
    diagnostics = _burst_diagnostics(
        acquisition,
        channel,
        equalized_header,
        equalized_payload,
        tracking,
        header=header,
        soft_result=soft_result,
        turbo_report=(None if frame_result is None else frame_result.fec_report),
        outer_crc_ok=True,
        decoder_latency_seconds=decoder_latency,
        decode_outcome="decoded",
    )
    return DecodedBurst(
        frame=frame,
        burst_start=acquisition.preamble_start,
        burst_end=observed_burst_end,
        diagnostics=diagnostics,
        payload_symbols=payload_symbols,
    )


def _apply_sample_clock_tracking(
    corrected_fixed: NDArray[np.complex64],
    corrected_payload_source: NDArray[np.complex64],
    *,
    payload_start_from_preamble: int,
    payload_sample_count: int,
    mcs: MCS,
    phase_slope_rad_per_carrier: ArrayLike,
    config: BurstConfig,
) -> tuple[NDArray[np.complex64], SampleClockTrackingDiagnostics]:
    """Resample the payload for sample-clock offset when the estimate warrants it.

    The two corrected segments are kept separate and only joined when a
    correction is actually applied: the common case reads the payload
    straight out of its own segment, so neither the join nor the per-sample
    timing vector is built.  The timing correction is affine in the sample
    index over a non-negative range, so its first, last and maximum absolute
    values are exact scalars.
    """

    tracking = config.sample_clock_tracking
    slopes = np.asarray(phase_slope_rad_per_carrier, dtype=np.float64)
    block_length = config.numerology.fft_size + config.numerology.cp_length
    slope_rate = 0.0
    estimated_ppm = 0.0
    if tracking.enabled and slopes.size >= tracking.minimum_payload_symbols:
        edge_count = max(2, slopes.size // 4)
        early_index = (edge_count - 1) / 2.0
        late_index = slopes.size - 1 - early_index
        slope_rate = float(
            (np.median(slopes[-edge_count:]) - np.median(slopes[:edge_count]))
            / (late_index - early_index)
        )
        timing_slew_per_symbol = (
            -slope_rate * config.numerology.fft_size / (2.0 * np.pi)
        )
        estimated_ppm = timing_slew_per_symbol / block_length * 1e6

    requested_ppm = tracking.loop_gain * estimated_ppm
    last_sample_from_preamble = payload_start_from_preamble + payload_sample_count - 1
    ppm_limits = (
        tracking.max_abs_correction_ppm,
        tracking.max_timing_slew_samples_per_symbol / block_length * 1e6,
        tracking.max_abs_timing_shift_samples / last_sample_from_preamble * 1e6,
    )
    applied_limit = min(ppm_limits)
    bounded_ppm = float(np.clip(requested_ppm, -applied_limit, applied_limit))
    limit_event_count = sum(abs(requested_ppm) > limit for limit in ppm_limits)
    # ``payload_start_from_preamble`` is positive and the indices only grow, so
    # the largest absolute correction is the one at the final sample.
    max_abs_bounded_correction = abs(last_sample_from_preamble * bounded_ppm * 1e-6)
    # Scale sample drift by sqrt(constellation order), a simple decision-distance
    # sensitivity proxy. This avoids a second FFT/resampling pass for low-risk
    # bursts while still activating at the committed 1272/1273-byte boundary.
    normalized_timing_risk = float(
        max_abs_bounded_correction * np.sqrt(1 << mcs.bits_per_symbol)
    )
    correction_applied = (
        bounded_ppm != 0.0
        and normalized_timing_risk >= tracking.minimum_normalized_timing_shift
    )
    applied_ppm = bounded_ppm if correction_applied else 0.0
    timing_correction_start = payload_start_from_preamble * applied_ppm * 1e-6
    timing_correction_end = last_sample_from_preamble * applied_ppm * 1e-6

    if applied_ppm == 0.0:
        corrected = corrected_payload_source[:payload_sample_count]
    else:
        payload_start_in_source = corrected_fixed.size
        sample_indices = np.arange(payload_sample_count, dtype=np.float64)
        absolute_indices = payload_start_from_preamble + sample_indices
        timing_correction = absolute_indices * applied_ppm * 1e-6
        source_positions = payload_start_in_source + sample_indices + timing_correction
        corrected = _linear_interpolate(
            np.concatenate((corrected_fixed, corrected_payload_source)),
            source_positions,
        )

    diagnostics = SampleClockTrackingDiagnostics(
        symbols_observed=int(slopes.size),
        observed_slope_rate_rad_per_carrier_per_symbol=slope_rate,
        estimated_sfo_ppm=estimated_ppm,
        applied_correction_ppm=applied_ppm,
        resampling_ratio=1.0 + applied_ppm * 1e-6,
        timing_correction_start_samples=float(timing_correction_start),
        timing_correction_end_samples=float(timing_correction_end),
        max_abs_timing_correction_samples=float(abs(timing_correction_end)),
        normalized_timing_risk=normalized_timing_risk,
        correction_applied=correction_applied,
        limit_event_count=limit_event_count,
        limit_hit=limit_event_count > 0,
    )
    return corrected, diagnostics


def _linear_interpolate(
    source: NDArray[np.complex64],
    positions: NDArray[np.float64],
) -> NDArray[np.complex64]:
    """Apply one bounded fractional-delay pass without Python per-sample work."""

    if np.min(positions) < 0.0 or np.max(positions) > source.size - 1.0:
        raise BurstDecodeError("timing correction requires unavailable payload samples")
    left = np.floor(positions).astype(np.int64)
    right = np.minimum(left + 1, source.size - 1)
    fraction = positions - left
    return (
        source[left] * (1.0 - fraction) + source[right] * fraction
    ).astype(np.complex64)


def _burst_diagnostics(
    acquisition: AcquisitionResult,
    channel: ChannelEstimate,
    equalized_header: EqualizedBatch,
    equalized_payload: EqualizedBatch,
    tracking: SampleClockTrackingDiagnostics,
    header: BurstHeaderDescriptor,
    soft_result: SoftDemapResult | None,
    turbo_report: TurboDecodeReport | None,
    outer_crc_ok: bool | None,
    decoder_latency_seconds: float,
    *,
    decode_outcome: str,
) -> BurstDiagnostics:
    turbo_mode = isinstance(header, TurboBurstHeader)
    return BurstDiagnostics(
        acquisition=acquisition,
        channel_estimate=channel,
        header_common_phase_rad=_finite_tuple(equalized_header.common_phase_rad),
        header_phase_slope_rad_per_carrier=_finite_tuple(
            equalized_header.phase_slope_rad_per_carrier
        ),
        payload_common_phase_rad=_finite_tuple(equalized_payload.common_phase_rad),
        payload_phase_slope_rad_per_carrier=_finite_tuple(
            equalized_payload.phase_slope_rad_per_carrier
        ),
        sample_clock_tracking=tracking,
        payload_pilot_magnitude=equalized_payload.magnitude_diagnostics,
        wire_version=header.wire_version,
        active_fec_profile=frame_codec_for_wire_version(
            header.wire_version
        ).fec_profile.name,
        decision_mode=(
            "turbo" if turbo_mode else ("soft" if soft_result is not None else "hard")
        ),
        code_block_count=(1 if turbo_report is None else turbo_report.block_count),
        filler_bit_count=(0 if turbo_report is None else turbo_report.filler_bit_count),
        configured_max_iterations=(
            0 if turbo_report is None else turbo_report.configured_max_iterations
        ),
        actual_iterations_per_block=(
            () if turbo_report is None else turbo_report.actual_iterations_per_block
        ),
        early_stop_reasons=(
            () if turbo_report is None else turbo_report.early_stop_reasons
        ),
        code_block_crc_ok=(
            () if turbo_report is None else turbo_report.code_block_crc_ok
        ),
        outer_crc_ok=outer_crc_ok,
        estimated_noise_variance=(
            None if soft_result is None else soft_result.noise_variance_mean
        ),
        llr_saturation_count=(
            0 if soft_result is None else soft_result.saturation_count
        ),
        llr_saturation_rate=(
            0.0 if soft_result is None else soft_result.saturation_rate
        ),
        decoder_latency_seconds=decoder_latency_seconds,
        decode_outcome=decode_outcome,
    )


def _validate_coded_payload_size(
    header: BurstHeaderDescriptor,
    config: BurstConfig,
) -> None:
    count = header.coded_payload_bit_count
    if count > config.max_coded_payload_bits:
        raise BurstDecodeError("signaled coded payload exceeds max_coded_payload_bits")
    if isinstance(header, TurboBurstHeader):
        return
    # Ask the profile itself for the shortest legal frame and the coded bits
    # one extra payload byte costs, so termination stays private to the adapter.
    profile = frame_codec_for_wire_version(header.wire_version).fec_profile
    minimum = profile.coded_bit_count(_MIN_FRAME_INFORMATION_BITS)
    stride = profile.coded_bit_count(_MIN_FRAME_INFORMATION_BITS + 8) - minimum
    if count < minimum or (count - minimum) % stride:
        raise BurstDecodeError("signaled coded payload length is not a valid frame size")


# Header plus CRC with an empty payload: the shortest frame the codec accepts.
_MIN_FRAME_INFORMATION_BITS = 11 * 8


def _is_legacy_v1(header: BurstHeaderDescriptor) -> bool:
    """Whether this burst uses the original single-version decoder call.

    Wire v1 keeps the bare positional call so any simple frame decoder stays a
    valid backend; every later version needs its wire version passed through.
    """

    return (
        isinstance(header, BurstHeader)
        and header.wire_version == CURRENT_BURST_HEADER_VERSION
    )


def _decoder_kwargs(header: BurstHeaderDescriptor) -> dict[str, object]:
    """Select the decode arguments this wire version needs."""

    if isinstance(header, TurboBurstHeader):
        return {
            "wire_version": header.wire_version,
            "information_bit_count": header.information_frame_bit_count,
        }
    return {"wire_version": header.wire_version}


def _map_with_backend(
    bits: NDArray[np.uint8],
    mcs: MCS,
    symbol_mapper: SymbolMapper,
) -> NDArray[np.complex64]:
    try:
        result = symbol_mapper(bits, mcs)
    except Exception as error:
        raise BurstValidationError(f"symbol_mapper failed: {error}") from error
    try:
        symbols = np.asarray(result)
    except (TypeError, ValueError) as error:
        raise BurstValidationError("symbol_mapper must return a numeric array") from error
    if symbols.ndim != 1:
        raise BurstValidationError("symbol_mapper must return a one-dimensional array")
    if not np.issubdtype(symbols.dtype, np.number):
        raise BurstValidationError("symbol_mapper must return a numeric array")
    expected_count = bits.size // mcs.bits_per_symbol
    if symbols.size != expected_count:
        raise BurstValidationError(
            f"symbol_mapper returned {symbols.size} symbols; expected {expected_count}"
        )
    converted = symbols.astype(np.complex64, copy=False)
    if not np.all(np.isfinite(converted.real)) or not np.all(np.isfinite(converted.imag)):
        raise BurstValidationError("symbol_mapper must return only finite symbols")
    return converted


def _header_llrs(symbols: NDArray[np.complex64]) -> NDArray[np.float64]:
    """Unweighted fixed-QPSK header LLRs; positive selects bit 0.

    The single-symbol training estimate makes per-carrier reliability weights
    noisy, so the soft header fallback combines equalized amplitudes directly.
    """

    llrs = np.empty(symbols.size * 2, dtype=np.float64)
    llrs[0::2] = symbols.real
    llrs[1::2] = symbols.imag
    return llrs


def _demap_with_backend(
    symbols: NDArray[np.complex64],
    mcs: MCS,
    symbol_demapper: SymbolDemapper,
) -> NDArray[np.uint8]:
    try:
        result = symbol_demapper(symbols, mcs)
    except Exception as error:
        raise BurstDecodeError(f"symbol_demapper failed: {error}") from error
    try:
        bits = np.asarray(result)
    except (TypeError, ValueError) as error:
        raise BurstDecodeError("symbol_demapper must return a numeric array") from error
    if bits.ndim != 1:
        raise BurstDecodeError("symbol_demapper must return a one-dimensional array")
    if not np.issubdtype(bits.dtype, np.number):
        raise BurstDecodeError("symbol_demapper must return a numeric array")
    expected_count = symbols.size * mcs.bits_per_symbol
    if bits.size != expected_count:
        raise BurstDecodeError(
            f"symbol_demapper returned {bits.size} bits; expected {expected_count}"
        )
    if not np.all((bits == 0) | (bits == 1)):
        raise BurstDecodeError("symbol_demapper must return only binary bits")
    return bits.astype(np.uint8, copy=False)


def _soft_demap_with_backend(
    symbols: NDArray[np.complex64],
    mcs: MCS,
    noise_variance: NDArray[np.float64],
    soft_symbol_demapper: SoftSymbolDemapper,
) -> SoftDemapResult:
    try:
        result = soft_symbol_demapper(symbols, mcs, noise_variance)
    except Exception as error:
        raise BurstDecodeError(f"soft_symbol_demapper failed: {error}") from error
    if not isinstance(result, SoftDemapResult):
        raise BurstDecodeError("soft_symbol_demapper must return a SoftDemapResult")
    expected_count = symbols.size * mcs.bits_per_symbol
    if result.llrs.size != expected_count:
        raise BurstDecodeError(
            f"soft_symbol_demapper returned {result.llrs.size} LLRs; "
            f"expected {expected_count}"
        )
    return result


def _correct_cfo(
    samples: NDArray[np.complex64],
    *,
    start_index: int,
    normalized_cfo: float,
    fft_size: int,
) -> NDArray[np.complex64]:
    # Reduce to a fraction of one turn in float64 before the float32 rotation.
    # The raw phase grows with sample index, and a burst is tens of thousands
    # of samples long, so float32 would lose the precision it still has near
    # zero.  np.exp on the complex argument ran the whole thing in complex128.
    # Each step below writes back into the same float64 buffer, so one burst
    # costs one temporary instead of one per arithmetic operation.
    phase = np.arange(
        start_index, start_index + samples.size, dtype=np.float64
    )
    phase *= normalized_cfo / fft_size
    np.remainder(phase, 1.0, out=phase)
    phase *= 2.0 * np.pi
    correction = conjugate_rotation(phase)
    return np.multiply(samples, correction, out=correction)


def _input_samples(samples: ArrayLike, config: BurstConfig) -> NDArray[np.complex64]:
    array = np.asarray(samples)
    if array.ndim != 1:
        raise BurstDecodeError("samples must be a one-dimensional array")
    if not np.issubdtype(array.dtype, np.number):
        raise BurstDecodeError("samples must be numeric")
    if array.size > config.max_input_samples:
        raise BurstDecodeError("input exceeds max_input_samples")
    converted = array.astype(np.complex64, copy=False)
    if not all_finite(converted):
        raise BurstDecodeError("samples must contain only finite values")
    return converted


def _frozen_samples(
    samples: ArrayLike,
    name: str,
    error_type: type[BurstError],
) -> NDArray[np.complex64]:
    array = np.asarray(samples)
    if array.ndim != 1 or not np.issubdtype(array.dtype, np.number):
        raise error_type(f"{name} must be a numeric one-dimensional array")
    frozen = np.array(array, dtype=np.complex64, copy=True)
    if not all_finite(frozen):
        raise error_type(f"{name} must contain only finite values")
    frozen.setflags(write=False)
    return frozen


def _finite_tuple(values: ArrayLike) -> tuple[float, ...]:
    # ``tolist`` converts the whole buffer in C; a per-element ``float()``
    # generator pays a Python call for every carrier of every burst.
    return tuple(np.asarray(values, dtype=np.float64).tolist())


def _require_config(config: BurstConfig | None) -> BurstConfig:
    if config is None:
        return BurstConfig()
    if not isinstance(config, BurstConfig):
        raise BurstValidationError("config must be a BurstConfig")
    return config

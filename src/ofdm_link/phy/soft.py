"""CPU/headless max-log soft demapping for the v1 constellations."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite
from .codec import MCS, PhyCodecError
from .fec import SOFT_LLR_CLIP

LLR_CONVENTION = "positive-log-p0-over-p1"
DEFAULT_LLR_CLIP = SOFT_LLR_CLIP
_MAX_SYMBOLS = 300_000
_DISTANCE_CHUNK_SYMBOLS = 8_192


@dataclass(frozen=True, slots=True)
class SoftDemapResult:
    """Bounded LLR output and scalar diagnostics for one symbol vector."""

    llrs: NDArray[np.float32]
    saturation_count: int
    saturation_rate: float
    noise_variance_min: float
    noise_variance_mean: float
    noise_variance_max: float

    def __post_init__(self) -> None:
        llrs = np.asarray(self.llrs)
        if llrs.ndim != 1 or llrs.dtype != np.float32:
            raise PhyCodecError("llrs must be a one-dimensional float32 array")
        if not np.all(np.isfinite(llrs)):
            raise PhyCodecError("llrs must contain only finite values")
        if type(self.saturation_count) is not int or not 0 <= self.saturation_count <= llrs.size:
            raise PhyCodecError("saturation_count must fit the LLR vector")
        if not np.isfinite(self.saturation_rate) or not 0.0 <= self.saturation_rate <= 1.0:
            raise PhyCodecError("saturation_rate must be finite and in [0, 1]")
        variances = (
            self.noise_variance_min,
            self.noise_variance_mean,
            self.noise_variance_max,
        )
        if not np.all(np.isfinite(variances)) or any(value <= 0.0 for value in variances):
            raise PhyCodecError("noise variance diagnostics must be positive and finite")
        frozen = np.array(llrs, dtype=np.float32, copy=True)
        frozen.setflags(write=False)
        object.__setattr__(self, "llrs", frozen)


def soft_demap_symbols(
    symbols: ArrayLike,
    mcs: MCS,
    complex_noise_variance: ArrayLike,
    *,
    llr_clip: float = DEFAULT_LLR_CLIP,
) -> SoftDemapResult:
    """Return max-log ``log(P(bit=0)/P(bit=1))`` LLRs in wire-bit order.

    ``complex_noise_variance`` is :math:`E[|n|^2]` after equalization, either
    one scalar or one value per constellation symbol.  Distances are divided
    by that complex variance.  The implementation is vectorized and chunks
    the 16QAM distance matrix to bound temporary memory.
    """

    mode = _require_mcs(mcs)
    values = _validated_symbols(symbols)
    clip = _positive_finite_scalar(llr_clip, "llr_clip")
    variances, variance_summary = _validated_variances(
        complex_noise_variance,
        int(values.size),
    )
    if not values.size:
        return SoftDemapResult(
            np.empty(0, dtype=np.float32),
            0,
            0.0,
            variance_summary[0],
            variance_summary[1],
            variance_summary[2],
        )

    if mode is MCS.QPSK:
        scale = (2.0 * np.sqrt(2.0)) / variances
        # Write the interleaved I/Q LLRs straight into their final positions:
        # ``column_stack`` built two full-size temporaries and then copied
        # both of them into a third array.
        raw = np.empty(values.size * 2, dtype=np.float64)
        np.multiply(values.real, scale, out=raw[0::2])
        np.multiply(values.imag, scale, out=raw[1::2])
    else:
        raw = _qam16_max_log_llrs(values, variances)

    saturation_count = int(np.count_nonzero(np.abs(raw) > clip))
    llrs = np.clip(raw, -clip, clip).astype(np.float32, copy=False)
    return SoftDemapResult(
        llrs,
        saturation_count,
        saturation_count / int(llrs.size),
        variance_summary[0],
        variance_summary[1],
        variance_summary[2],
    )


def llrs_to_hard_bits(llrs: ArrayLike) -> NDArray[np.uint8]:
    """Slice the project convention: negative selects bit 1, else bit 0."""

    values = np.asarray(llrs)
    if values.ndim != 1 or not np.issubdtype(values.dtype, np.number):
        raise PhyCodecError("llrs must be a numeric one-dimensional array")
    converted = values.astype(np.float64, copy=False)
    if not np.all(np.isfinite(converted)):
        raise PhyCodecError("llrs must contain only finite values")
    return (converted < 0.0).astype(np.uint8)


def _qam16_max_log_llrs(
    symbols: NDArray[np.complex128],
    variances: NDArray[np.float64],
) -> NDArray[np.float64]:
    axis_pairs = np.array(((0, 0), (0, 1), (1, 1), (1, 0)), dtype=np.uint8)
    axis_levels = np.array((-3.0, -1.0, 1.0, 3.0), dtype=np.float64) / np.sqrt(10.0)
    labels = np.column_stack(
        (
            np.repeat(axis_pairs, 4, axis=0),
            np.tile(axis_pairs, (4, 1)),
        )
    )
    constellation = (
        np.repeat(axis_levels, 4) + 1j * np.tile(axis_levels, 4)
    ).astype(np.complex128)
    output = np.empty((symbols.size, 4), dtype=np.float64)
    for start in range(0, symbols.size, _DISTANCE_CHUNK_SYMBOLS):
        stop = min(start + _DISTANCE_CHUNK_SYMBOLS, symbols.size)
        distances = np.abs(symbols[start:stop, None] - constellation[None, :]) ** 2
        for bit_index in range(4):
            minimum_zero = np.min(distances[:, labels[:, bit_index] == 0], axis=1)
            minimum_one = np.min(distances[:, labels[:, bit_index] == 1], axis=1)
            output[start:stop, bit_index] = (
                minimum_one - minimum_zero
            ) / variances[start:stop]
    return output.reshape(-1)


def _validated_symbols(symbols: ArrayLike) -> NDArray[np.complex128]:
    values = np.asarray(symbols)
    if values.ndim != 1 or not np.issubdtype(values.dtype, np.number):
        raise PhyCodecError("symbols must be a numeric one-dimensional array")
    if values.size > _MAX_SYMBOLS:
        raise PhyCodecError(f"symbols exceeds the {_MAX_SYMBOLS}-symbol bound")
    converted = values.astype(np.complex128, copy=False)
    if not all_finite(converted):
        raise PhyCodecError("symbols must contain only finite values")
    return converted


def _validated_variances(
    complex_noise_variance: ArrayLike,
    symbol_count: int,
) -> tuple[NDArray[np.float64], tuple[float, float, float]]:
    values = np.asarray(complex_noise_variance, dtype=np.float64)
    if values.ndim == 0:
        scalar = _positive_finite_scalar(float(values), "complex_noise_variance")
        return np.full(symbol_count, scalar, dtype=np.float64), (scalar, scalar, scalar)
    if values.ndim != 1 or values.size != symbol_count:
        raise PhyCodecError(
            "complex_noise_variance must be a scalar or match the symbol count"
        )
    if not np.all(np.isfinite(values)) or np.any(values <= 0.0):
        raise PhyCodecError("complex_noise_variance must be positive and finite")
    if not symbol_count:
        raise PhyCodecError("empty symbols require a scalar complex_noise_variance")
    return values, (float(np.min(values)), float(np.mean(values)), float(np.max(values)))


def _positive_finite_scalar(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PhyCodecError(f"{name} must be a positive finite number")
    converted = float(value)
    if not np.isfinite(converted) or converted <= 0.0:
        raise PhyCodecError(f"{name} must be a positive finite number")
    return converted


def _require_mcs(mcs: MCS) -> MCS:
    if not isinstance(mcs, MCS):
        raise PhyCodecError("mcs must be an MCS")
    return mcs

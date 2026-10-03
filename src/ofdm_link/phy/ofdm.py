"""Deterministic aligned OFDM resource-grid modulation.

Carrier numbers use the conventional signed indexing around DC.  A carrier
number ``k`` maps to NumPy FFT bin ``k % fft_size``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray


class OfdmError(ValueError):
    """Raised when an aligned OFDM input violates the resource-grid contract."""


_DEFAULT_PILOTS = (-21, -7, 7, 21)
_DEFAULT_DATA = tuple(
    carrier
    for carrier in range(-26, 27)
    if carrier != 0 and carrier not in _DEFAULT_PILOTS
)
_PILOT_BASE = (1, 1, 1, -1)
_PILOT_POLARITY_V1 = (1, 1, 1, -1, -1, 1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1)


@dataclass(frozen=True, slots=True)
class OfdmNumerology:
    """Immutable 802.11a-like carrier allocation used by this project."""

    fft_size: int = 64
    cp_length: int = 16
    data_carriers: tuple[int, ...] = _DEFAULT_DATA
    pilot_carriers: tuple[int, ...] = _DEFAULT_PILOTS

    def __post_init__(self) -> None:
        data_carriers = tuple(self.data_carriers)
        pilot_carriers = tuple(self.pilot_carriers)
        object.__setattr__(self, "data_carriers", data_carriers)
        object.__setattr__(self, "pilot_carriers", pilot_carriers)

        if type(self.fft_size) is not int or self.fft_size <= 0 or self.fft_size % 2:
            raise OfdmError("fft_size must be a positive even integer")
        if (
            type(self.cp_length) is not int
            or self.cp_length <= 0
            or self.cp_length >= self.fft_size
        ):
            raise OfdmError("cp_length must be an integer in [1, fft_size)")
        if len(data_carriers) != 48:
            raise OfdmError("data_carriers must contain exactly 48 carriers")
        if len(pilot_carriers) != 4:
            raise OfdmError("pilot_carriers must contain exactly 4 carriers")

        active = data_carriers + pilot_carriers
        if any(type(carrier) is not int for carrier in active):
            raise OfdmError("carrier numbers must be integers")
        if len(set(active)) != len(active):
            raise OfdmError("data and pilot carriers must be unique and disjoint")
        lower, upper = -self.fft_size // 2, self.fft_size // 2 - 1
        if any(carrier < lower or carrier > upper for carrier in active):
            raise OfdmError(f"carrier numbers must be in [{lower}, {upper}]")
        if self.dc_carrier in active:
            raise OfdmError("DC carrier 0 must be unallocated")

    @property
    def dc_carrier(self) -> int:
        """The unallocated direct-current carrier."""

        return 0

    @property
    def guard_carriers(self) -> tuple[int, ...]:
        """All unallocated carriers other than DC, in signed order."""

        active = set(self.data_carriers) | set(self.pilot_carriers)
        return tuple(
            carrier
            for carrier in range(-self.fft_size // 2, self.fft_size // 2)
            if carrier != self.dc_carrier and carrier not in active
        )

    @property
    def active_carriers(self) -> tuple[int, ...]:
        """Data and pilot carriers in ascending signed-carrier order."""

        return tuple(sorted(self.data_carriers + self.pilot_carriers))


_DEFAULT_NUMEROLOGY = OfdmNumerology()


@dataclass(frozen=True, slots=True)
class OfdmWaveform:
    """Serialized time samples and exact data-padding metadata."""

    samples: NDArray[np.complex64]
    payload_symbol_count: int
    padding_symbol_count: int
    ofdm_symbol_count: int


def pilot_symbols(
    symbol_count: int,
    *,
    first_symbol_index: int = 0,
    version: int = 1,
) -> NDArray[np.complex64]:
    """Return the four deterministic pilot values for consecutive symbols.

    Version 1 repeats a fixed 16-symbol polarity sequence and applies it to
    the base pilot vector ``(+1, +1, +1, -1)``.  The explicit version is part
    of the project's wire contract rather than a NumPy or GNU Radio default.
    """

    if type(symbol_count) is not int or symbol_count < 0:
        raise OfdmError("symbol_count must be a non-negative integer")
    if type(first_symbol_index) is not int or first_symbol_index < 0:
        raise OfdmError("first_symbol_index must be a non-negative integer")
    if version != 1:
        raise OfdmError(f"unsupported pilot polarity version {version}")

    sequence = np.asarray(_PILOT_POLARITY_V1, dtype=np.float32)
    indices = (first_symbol_index + np.arange(symbol_count)) % sequence.size
    polarity = sequence[indices, np.newaxis]
    base = np.asarray(_PILOT_BASE, dtype=np.float32)[np.newaxis, :]
    return (polarity * base).astype(np.complex64)


def modulate_ofdm(
    payload_symbols: ArrayLike,
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
    *,
    first_symbol_index: int = 0,
    pilot_version: int = 1,
) -> OfdmWaveform:
    """Allocate symbols and pilots, then apply a unitary IFFT and cyclic prefix."""

    payload = _as_complex_vector(payload_symbols, "payload_symbols")
    if payload.size == 0:
        raise OfdmError("payload_symbols must not be empty")
    if not isinstance(numerology, OfdmNumerology):
        raise OfdmError("numerology must be an OfdmNumerology")

    data_per_symbol = len(numerology.data_carriers)
    symbol_count = (payload.size + data_per_symbol - 1) // data_per_symbol
    padded_count = symbol_count * data_per_symbol
    padding_count = padded_count - payload.size
    padded = np.pad(payload, (0, padding_count))

    grid = np.zeros((symbol_count, numerology.fft_size), dtype=np.complex64)
    data_bins = np.asarray(numerology.data_carriers) % numerology.fft_size
    pilot_bins = np.asarray(numerology.pilot_carriers) % numerology.fft_size
    grid[:, data_bins] = padded.reshape(symbol_count, data_per_symbol)
    grid[:, pilot_bins] = pilot_symbols(
        symbol_count,
        first_symbol_index=first_symbol_index,
        version=pilot_version,
    )

    useful = np.fft.ifft(grid, axis=1, norm="ortho")
    with_prefix = np.concatenate((useful[:, -numerology.cp_length :], useful), axis=1)
    samples = with_prefix.astype(np.complex64, copy=False).reshape(-1)
    samples.setflags(write=False)
    return OfdmWaveform(
        samples=samples,
        payload_symbol_count=payload.size,
        padding_symbol_count=padding_count,
        ofdm_symbol_count=symbol_count,
    )


def demodulate_ofdm(
    samples: ArrayLike,
    *,
    payload_symbol_count: int,
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
    channel_estimate: ArrayLike | None = None,
) -> NDArray[np.complex64]:
    """Remove CP/FFT, optionally equalize, and extract payload symbols.

    ``channel_estimate`` contains one complex coefficient for each
    ``numerology.active_carriers`` entry, in that signed-carrier order.  It is
    constant across the OFDM symbols in this aligned batch.
    """

    values = _as_complex_vector(samples, "samples")
    if not isinstance(numerology, OfdmNumerology):
        raise OfdmError("numerology must be an OfdmNumerology")
    block_length = numerology.fft_size + numerology.cp_length
    if values.size == 0 or values.size % block_length:
        raise OfdmError(f"samples length must be a positive multiple of {block_length}")

    symbol_count = values.size // block_length
    capacity = symbol_count * len(numerology.data_carriers)
    if (
        type(payload_symbol_count) is not int
        or payload_symbol_count <= 0
        or payload_symbol_count > capacity
    ):
        raise OfdmError(f"payload_symbol_count must be an integer in [1, {capacity}]")

    blocks = values.reshape(symbol_count, block_length)
    useful = blocks[:, numerology.cp_length :]
    grid = np.fft.fft(useful, axis=1, norm="ortho")
    active_bins = np.asarray(numerology.active_carriers) % numerology.fft_size
    if channel_estimate is not None:
        channel = _as_complex_vector(channel_estimate, "channel_estimate")
        if channel.size != len(numerology.active_carriers):
            raise OfdmError(
                "channel_estimate must contain one value per active carrier "
                f"({len(numerology.active_carriers)})"
            )
        if np.any(np.abs(channel) < 1e-8):
            raise OfdmError("channel_estimate contains a zero or near-zero value")
        grid[:, active_bins] /= channel[np.newaxis, :]

    data_bins = np.asarray(numerology.data_carriers) % numerology.fft_size
    recovered = grid[:, data_bins].reshape(-1)[:payload_symbol_count]
    return recovered.astype(np.complex64, copy=False)


def _as_complex_vector(values: ArrayLike, name: str) -> NDArray[np.complex64]:
    array = np.asarray(values)
    if array.ndim != 1:
        raise OfdmError(f"{name} must be a one-dimensional array")
    if not np.issubdtype(array.dtype, np.number):
        raise OfdmError(f"{name} must be numeric")
    converted = array.astype(np.complex64, copy=False)
    if not np.all(np.isfinite(converted.real)) or not np.all(np.isfinite(converted.imag)):
        raise OfdmError(f"{name} must contain only finite values")
    return converted

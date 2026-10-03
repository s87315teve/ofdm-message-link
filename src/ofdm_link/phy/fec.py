"""Small PHY-internal FEC interface and the stable convolutional adapter.

The public contract deliberately describes only information bits, coded
observations, and length invariants.  Polynomial, state, and termination
details remain private to the adapter so a future codec can replace it without
leaking implementation choices into MAC, network, GUI, or runtime callers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import ArrayLike, NDArray

_CONSTRAINT_LENGTH = 7
_TAIL_BITS = _CONSTRAINT_LENGTH - 1
_STATE_COUNT = 1 << (_CONSTRAINT_LENGTH - 1)
_GENERATORS = (0o133, 0o171)
_RATE_INVERSE = len(_GENERATORS)
SOFT_LLR_CLIP = 64.0

# Mother codes only: each entry is a distinct K=7 generator set, never a
# punctured derivative of another rate.  Rate matching remains out of scope, so
# no rate between these values is reachable.
_GENERATORS_BY_RATE_INVERSE: dict[int, tuple[int, ...]] = {
    2: _GENERATORS,
    3: (0o133, 0o171, 0o165),
}
CONVOLUTIONAL_RATE_INVERSES = tuple(sorted(_GENERATORS_BY_RATE_INVERSE))


def _generators_for(rate_inverse: int) -> tuple[int, ...]:
    """Return the mother-code generators for one supported rate 1/N."""

    try:
        return _GENERATORS_BY_RATE_INVERSE[rate_inverse]
    except KeyError:
        raise ValueError(
            f"unsupported convolutional rate 1/{rate_inverse}; "
            f"supported: {sorted(_GENERATORS_BY_RATE_INVERSE)}"
        ) from None


def _convolutional_profile_name(rate_inverse: int) -> str:
    """Name one mother code by its rate and octal generators."""

    generators = "/".join(f"{value:o}" for value in _generators_for(rate_inverse))
    return f"convolutional-k7-r1/{rate_inverse}-{generators}"


class FecError(ValueError):
    """Raised when a FEC profile rejects input or decoder output."""

    def __init__(self, message: str, *, decode_report: object | None = None) -> None:
        super().__init__(message)
        self.decode_report = decode_report


@dataclass(frozen=True, slots=True)
class FecProfile:
    """Immutable caller-visible invariants for one complete FEC adapter."""

    name: str
    decision_mode: Literal["hard", "soft", "turbo"]
    max_information_bits: int
    _coded_bit_counter: Callable[[int], int] | None = None
    _rate_inverse: int = _RATE_INVERSE
    _tail_bits: int = _TAIL_BITS

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if self.decision_mode not in {"hard", "soft", "turbo"}:
            raise ValueError("decision_mode must be 'hard', 'soft', or 'turbo'")
        if (
            type(self.max_information_bits) is not int
            or self.max_information_bits < 0
        ):
            raise ValueError("max_information_bits must be a non-negative integer")
        if type(self._rate_inverse) is not int or self._rate_inverse < 1:
            raise ValueError("_rate_inverse must be a positive integer")
        if type(self._tail_bits) is not int or self._tail_bits < 0:
            raise ValueError("_tail_bits must be a non-negative integer")

    @property
    def rate_inverse(self) -> int:
        """Coded values transmitted per information bit, ignoring termination.

        Fixed-rate profiles report their mother-code rate 1/N as N.  The Turbo
        profile computes length through its own counter, so this only names the
        nominal rate there.
        """

        return self._rate_inverse

    def coded_bit_count(self, information_bit_count: int) -> int:
        """Return the exact coded length after private termination semantics."""

        count = self.validate_information_bit_count(information_bit_count)
        if self._coded_bit_counter is not None:
            return self._coded_bit_counter(count)
        return self._rate_inverse * (count + self._tail_bits)

    def information_bit_count(self, coded_bit_count: int) -> int:
        """Validate and invert the current rate/termination length relation."""

        if type(coded_bit_count) is not int:
            raise FecError("coded_bit_count must be an integer")
        if self._coded_bit_counter is not None:
            raise FecError(
                "this FEC profile requires an explicit information_bit_count"
            )
        minimum = self._rate_inverse * self._tail_bits
        maximum = self.coded_bit_count(self.max_information_bits)
        if coded_bit_count < minimum:
            raise FecError(
                f"coded input is too short: got {coded_bit_count}, need at least {minimum}"
            )
        if coded_bit_count > maximum:
            raise FecError(
                f"coded input is too long: got {coded_bit_count}, maximum is {maximum}"
            )
        if coded_bit_count % self._rate_inverse:
            if self._rate_inverse == 2:
                raise FecError(
                    "rate-1/2 coded input must contain an even number of values"
                )
            raise FecError(
                f"rate-1/{self._rate_inverse} coded input must contain a multiple of "
                f"{self._rate_inverse} values"
            )
        return coded_bit_count // self._rate_inverse - self._tail_bits

    def validate_information_bit_count(self, information_bit_count: int) -> int:
        """Validate a bounded information length and return it unchanged."""

        if type(information_bit_count) is not int:
            raise FecError("information_bit_count must be an integer")
        if not 0 <= information_bit_count <= self.max_information_bits:
            raise FecError(
                "information_bit_count must be in "
                f"[0, {self.max_information_bits}]"
            )
        return information_bit_count


@runtime_checkable
class FecCodec(Protocol):
    """Complete encoder/decoder seam used by the frame codec."""

    @property
    def profile(self) -> FecProfile: ...

    def encode(self, information_bits: ArrayLike) -> NDArray[np.uint8]: ...

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]: ...


HardDecoder = Callable[[NDArray[np.uint8]], ArrayLike]
SoftDecoder = Callable[[NDArray[np.float32]], ArrayLike]


class ConvolutionalFecAdapter:
    """Stable K=7 133/171 adapter at rate 1/2, or a rate-1/3 mother code.

    The default decoder is the portable hard-decision NumPy reference.  A
    native decoder may be injected while encoding, validation, termination,
    and output semantics remain one coherent adapter contract.  ``rate_inverse``
    selects a distinct mother code, never a punctured derivative, so rate 1/2
    stays bit-exact with the stable wire-v1 contract.
    """

    def __init__(
        self,
        *,
        max_information_bits: int,
        hard_decoder: HardDecoder | None = None,
        rate_inverse: int = _RATE_INVERSE,
    ) -> None:
        generators = _generators_for(rate_inverse)
        self._generators = generators
        self._profile = FecProfile(
            name=_convolutional_profile_name(rate_inverse),
            decision_mode="hard",
            max_information_bits=max_information_bits,
            _rate_inverse=rate_inverse,
        )
        if hard_decoder is not None and not callable(hard_decoder):
            raise TypeError("hard_decoder must be callable or None")
        self._hard_decoder = hard_decoder or (
            lambda coded: _viterbi_decode(coded, generators)
        )

    @property
    def profile(self) -> FecProfile:
        """Return the immutable descriptor for this adapter."""

        return self._profile

    def encode(self, information_bits: ArrayLike) -> NDArray[np.uint8]:
        """Encode information bits, owning termination and output ordering."""

        bits = _as_bits(information_bits, "information_bits")
        self.profile.validate_information_bit_count(int(bits.size))
        return _convolutional_encode(bits, self._generators)

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]:
        """Decode hard observations and return information bits without tail."""

        coded = _as_bits(observations, "observations")
        information_count = self.profile.information_bit_count(int(coded.size))
        if information_bit_count is not None:
            expected = self.profile.validate_information_bit_count(information_bit_count)
            if information_count != expected:
                raise FecError(
                    f"coded input represents {information_count} information bits; "
                    f"expected {expected}"
                )
        return _validated_information_output(
            self._hard_decoder(coded),
            information_count,
        )


class SoftConvolutionalFecAdapter:
    """Convolutional encoder plus float-LLR decoder using the project sign."""

    def __init__(
        self,
        *,
        max_information_bits: int,
        soft_decoder: SoftDecoder | None = None,
        rate_inverse: int = _RATE_INVERSE,
    ) -> None:
        generators = _generators_for(rate_inverse)
        self._generators = generators
        self._profile = FecProfile(
            name=_convolutional_profile_name(rate_inverse),
            decision_mode="soft",
            max_information_bits=max_information_bits,
            _rate_inverse=rate_inverse,
        )
        if soft_decoder is not None and not callable(soft_decoder):
            raise TypeError("soft_decoder must be callable or None")
        self._soft_decoder = soft_decoder or (
            lambda llrs: _soft_viterbi_decode(llrs, generators)
        )

    @property
    def profile(self) -> FecProfile:
        """Return the immutable descriptor for this adapter."""

        return self._profile

    def encode(self, information_bits: ArrayLike) -> NDArray[np.uint8]:
        """Preserve the exact stable convolutional coded-bit representation."""

        bits = _as_bits(information_bits, "information_bits")
        self.profile.validate_information_bit_count(int(bits.size))
        return _convolutional_encode(bits, self._generators)

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]:
        """Decode finite LLRs where positive means bit 0."""

        llrs = _as_llrs(observations, "observations")
        information_count = self.profile.information_bit_count(int(llrs.size))
        if information_bit_count is not None:
            expected = self.profile.validate_information_bit_count(information_bit_count)
            if information_count != expected:
                raise FecError(
                    f"coded input represents {information_count} information bits; "
                    f"expected {expected}"
                )
        return _validated_information_output(
            self._soft_decoder(llrs),
            information_count,
        )


class UncodedFecAdapter:
    """Rate-1 adapter that adds no redundancy and no termination.

    This is the one profile with no error-correction capability: the frame
    layer's outer CRC-32 still detects corruption, but nothing repairs it.  It
    exists so the MCS table has a defined upper spectral-efficiency bound
    without introducing rate matching.
    """

    def __init__(self, *, max_information_bits: int) -> None:
        self._profile = FecProfile(
            name="uncoded-r1",
            decision_mode="hard",
            max_information_bits=max_information_bits,
            _rate_inverse=1,
            _tail_bits=0,
        )

    @property
    def profile(self) -> FecProfile:
        """Return the immutable descriptor for this adapter."""

        return self._profile

    def encode(self, information_bits: ArrayLike) -> NDArray[np.uint8]:
        """Return the information bits unchanged as the coded representation."""

        bits = _as_bits(information_bits, "information_bits")
        self.profile.validate_information_bit_count(int(bits.size))
        result = np.array(bits, dtype=np.uint8, copy=True)
        result.setflags(write=False)
        return result

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]:
        """Validate the length relation and return the observed bits."""

        coded = _as_bits(observations, "observations")
        information_count = self.profile.information_bit_count(int(coded.size))
        if information_bit_count is not None:
            expected = self.profile.validate_information_bit_count(information_bit_count)
            if information_count != expected:
                raise FecError(
                    f"coded input represents {information_count} information bits; "
                    f"expected {expected}"
                )
        return _validated_information_output(coded, information_count, 0)


class SoftUncodedFecAdapter(UncodedFecAdapter):
    """Rate-1 adapter taking project-convention LLRs, where positive means 0."""

    def __init__(self, *, max_information_bits: int) -> None:
        super().__init__(max_information_bits=max_information_bits)
        self._profile = FecProfile(
            name="uncoded-r1",
            decision_mode="soft",
            max_information_bits=max_information_bits,
            _rate_inverse=1,
            _tail_bits=0,
        )

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]:
        """Slice finite LLRs to bits without any trellis search."""

        llrs = _as_llrs(observations, "observations")
        information_count = self.profile.information_bit_count(int(llrs.size))
        if information_bit_count is not None:
            expected = self.profile.validate_information_bit_count(information_bit_count)
            if information_count != expected:
                raise FecError(
                    f"coded input represents {information_count} information bits; "
                    f"expected {expected}"
                )
        decided = (llrs < 0.0).astype(np.uint8)
        return _validated_information_output(decided, information_count, 0)


def _convolutional_encode(
    bits: NDArray[np.uint8],
    generators: tuple[int, ...] = _GENERATORS,
) -> NDArray[np.uint8]:
    terminated = np.pad(bits, (0, _TAIL_BITS))
    outputs = np.zeros((terminated.size, len(generators)), dtype=np.uint8)
    for output_index, generator in enumerate(generators):
        parity = np.zeros(terminated.size, dtype=np.uint8)
        for delay in range(_CONSTRAINT_LENGTH):
            if generator & (1 << delay):
                if delay == 0:
                    parity ^= terminated
                else:
                    parity[delay:] ^= terminated[:-delay]
        outputs[:, output_index] = parity
    return outputs.reshape(-1)


def _viterbi_decode(
    coded: NDArray[np.uint8],
    generators: tuple[int, ...] = _GENERATORS,
) -> NDArray[np.uint8]:
    received = coded.reshape(-1, len(generators))
    destination = np.arange(_STATE_COUNT, dtype=np.uint8)
    input_bits = destination & 1
    predecessor_a = destination >> 1
    predecessor_b = predecessor_a | (_STATE_COUNT >> 1)

    output_table = np.empty((_STATE_COUNT, 2, len(generators)), dtype=np.uint8)
    for state in range(_STATE_COUNT):
        for input_bit in (0, 1):
            register = (state << 1) | input_bit
            output_table[state, input_bit] = [
                (register & generator).bit_count() & 1 for generator in generators
            ]

    infinity = np.iinfo(np.int32).max
    metrics = np.full(_STATE_COUNT, infinity, dtype=np.int64)
    metrics[0] = 0
    predecessors = np.empty((received.shape[0], _STATE_COUNT), dtype=np.uint8)

    for time_index, received_pair in enumerate(received):
        branch_a = np.count_nonzero(
            output_table[predecessor_a, input_bits] != received_pair, axis=1
        )
        branch_b = np.count_nonzero(
            output_table[predecessor_b, input_bits] != received_pair, axis=1
        )
        candidate_a = metrics[predecessor_a] + branch_a
        candidate_b = metrics[predecessor_b] + branch_b
        choose_a = candidate_a <= candidate_b
        metrics = np.where(choose_a, candidate_a, candidate_b)
        predecessors[time_index] = np.where(
            choose_a, predecessor_a, predecessor_b
        ).astype(np.uint8)

    decoded = np.empty(received.shape[0], dtype=np.uint8)
    state = 0
    for time_index in range(received.shape[0] - 1, -1, -1):
        decoded[time_index] = state & 1
        state = int(predecessors[time_index, state])
    return decoded


def _soft_viterbi_decode(
    llrs: NDArray[np.float32],
    generators: tuple[int, ...] = _GENERATORS,
) -> NDArray[np.uint8]:
    """Portable correctness oracle; production uses GNU Radio native Viterbi."""

    received = llrs.astype(np.float64, copy=False).reshape(-1, len(generators))
    destination = np.arange(_STATE_COUNT, dtype=np.uint8)
    input_bits = destination & 1
    predecessor_a = destination >> 1
    predecessor_b = predecessor_a | (_STATE_COUNT >> 1)
    output_table = np.empty((_STATE_COUNT, 2, len(generators)), dtype=np.uint8)
    for state in range(_STATE_COUNT):
        for input_bit in (0, 1):
            register = (state << 1) | input_bit
            output_table[state, input_bit] = [
                (register & generator).bit_count() & 1 for generator in generators
            ]

    metrics = np.full(_STATE_COUNT, np.inf, dtype=np.float64)
    metrics[0] = 0.0
    predecessors = np.empty((received.shape[0], _STATE_COUNT), dtype=np.uint8)
    for time_index, received_pair in enumerate(received):
        expected_a = output_table[predecessor_a, input_bits]
        expected_b = output_table[predecessor_b, input_bits]
        signs_a = 1.0 - 2.0 * expected_a
        signs_b = 1.0 - 2.0 * expected_b
        branch_a = np.logaddexp(0.0, -signs_a * received_pair).sum(axis=1)
        branch_b = np.logaddexp(0.0, -signs_b * received_pair).sum(axis=1)
        candidate_a = metrics[predecessor_a] + branch_a
        candidate_b = metrics[predecessor_b] + branch_b
        choose_a = candidate_a <= candidate_b
        metrics = np.where(choose_a, candidate_a, candidate_b)
        predecessors[time_index] = np.where(
            choose_a, predecessor_a, predecessor_b
        ).astype(np.uint8)

    decoded = np.empty(received.shape[0], dtype=np.uint8)
    state = 0
    for time_index in range(received.shape[0] - 1, -1, -1):
        decoded[time_index] = state & 1
        state = int(predecessors[time_index, state])
    return decoded


def _validated_information_output(
    decoded_bits: ArrayLike,
    information_count: int,
    tail_bits: int = _TAIL_BITS,
) -> NDArray[np.uint8]:
    decoded = _as_bits(decoded_bits, "decoder output")
    expected = information_count + tail_bits
    if decoded.size != expected:
        raise FecError(
            f"decoder returned {decoded.size} bits; expected exactly {expected}"
        )
    tail = decoded[information_count:]
    if np.any(tail):
        raise FecError("decoder output does not end in the required zero-state tail")
    result = np.array(decoded[:information_count], dtype=np.uint8, copy=True)
    result.setflags(write=False)
    return result


def _as_llrs(values: ArrayLike, name: str) -> NDArray[np.float32]:
    llrs = np.asarray(values)
    if llrs.ndim != 1 or not np.issubdtype(llrs.dtype, np.number):
        raise FecError(f"{name} must be a numeric one-dimensional array")
    converted = llrs.astype(np.float32, copy=False)
    if not np.all(np.isfinite(converted)):
        raise FecError(f"{name} must contain only finite LLRs")
    return converted


def _as_bits(values: ArrayLike, name: str) -> NDArray[np.uint8]:
    bits = np.asarray(values)
    if bits.ndim != 1:
        raise FecError(f"{name} must be a one-dimensional array")
    if not (
        np.issubdtype(bits.dtype, np.integer)
        or np.issubdtype(bits.dtype, np.bool_)
    ):
        raise FecError(f"{name} must contain integer bits")
    if np.any((bits != 0) & (bits != 1)):
        raise FecError(f"{name} must contain only 0 and 1")
    return bits.astype(np.uint8, copy=False)

"""Native transmit steps (_tx_native) against the Python reference.

Rate matching and mapping must be bit-identical; OFDM modulation (double
IFFT rounded once to complex64) within 1e-6 of NumPy's single-precision
transform; a whole burst must decode to the same body either way.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import (
    BurstConfig,
    PhyCodecError,
    encode_burst_v5,
    mcs_entry,
    native_backend,
)
from ofdm_link.phy.burst import _decode_burst_with_acquisition
from ofdm_link.phy.codec import DEFAULT_DUAL_VERSION_FRAME_DECODER, MCS, demap_symbols, map_symbols
from ofdm_link.phy.ofdm import OfdmNumerology, modulate_ofdm
from ofdm_link.phy.rate_match import rate_match, rate_match_plan
from ofdm_link.phy.sync import acquire_all

pytest.importorskip("ofdm_link.phy._tx_native")


@pytest.fixture(autouse=True)
def _compare_both_backends(monkeypatch):
    """These tests compare native with Python themselves; a global OFDM_LINK_NATIVE=python
    (used to run the whole suite on the reference path) must not turn both sides Python."""

    monkeypatch.delenv(native_backend.GLOBAL_ENV, raising=False)
    native_backend.reset()
    yield
    native_backend.reset()


@pytest.fixture
def backend(monkeypatch):
    def use(choice):
        monkeypatch.setenv(native_backend.env_name("tx"), choice)
        native_backend.reset()

    yield use
    native_backend.reset()


@pytest.mark.parametrize(
    ("information_bits", "available_bits", "qm"),
    [(320, 960, 2), (7200, 14400, 4), (7200, 9600, 6), (65600, 86400, 6), (40, 4800, 2)],
)
def test_rate_match_is_bit_identical(backend, information_bits, available_bits, qm):
    plan = rate_match_plan(information_bits, available_bits, qm)
    coded = np.random.default_rng(qm).integers(0, 2, plan.native_bit_count, dtype=np.uint8)
    backend("python")
    expected = rate_match(coded, plan)
    backend("native")
    got = rate_match(coded, plan)
    assert np.array_equal(got, expected) and got.dtype == np.uint8 and not got.flags.writeable
    assert np.array_equal(rate_match(coded, plan), expected)  # cached index


@pytest.mark.parametrize("mcs", [MCS.QPSK, MCS.QAM16, MCS.QAM64])
def test_map_symbols_is_bit_identical(backend, mcs):
    rng = np.random.default_rng(int(mcs))
    bits = rng.integers(0, 2, 6000 * mcs.bits_per_symbol, dtype=np.uint8)
    backend("python")
    expected = map_symbols(bits, mcs)
    backend("native")
    for given in (bits, bits.astype(bool), bits.astype(np.int64)):
        got = map_symbols(given, mcs)
        assert got.dtype == np.complex64 and np.array_equal(got.view(np.uint64),
                                                            expected.view(np.uint64))
    assert map_symbols(np.zeros(0, np.uint8), mcs).size == 0
    assert np.array_equal(demap_symbols(map_symbols(bits, mcs), mcs), bits)
    bad = bits.copy()
    bad[3] = 2
    with pytest.raises(PhyCodecError):
        map_symbols(bad, mcs)
    with pytest.raises(PhyCodecError):
        map_symbols(bits[:-1], mcs)


@pytest.mark.parametrize("count", [1, 47, 48, 49, 4800, 14403])
@pytest.mark.parametrize("first", [0, 3, 15, 1000])
def test_modulate_matches_within_float32_rounding(backend, count, first):
    rng = np.random.default_rng(count + first)
    symbols = ((rng.standard_normal(count) + 1j * rng.standard_normal(count)) / 2).astype(
        np.complex64
    )
    backend("python")
    expected = modulate_ofdm(symbols, first_symbol_index=first)
    backend("native")
    got = modulate_ofdm(symbols, first_symbol_index=first)
    assert (got.payload_symbol_count, got.padding_symbol_count, got.ofdm_symbol_count) == (
        expected.payload_symbol_count, expected.padding_symbol_count, expected.ofdm_symbol_count
    )
    assert got.samples.dtype == np.complex64 and not got.samples.flags.writeable
    assert np.max(np.abs(got.samples - expected.samples)) <= 1e-6


def test_modulate_rejects_what_the_python_path_rejects(backend):
    backend("native")
    from ofdm_link.phy.ofdm import OfdmError

    with pytest.raises(OfdmError):
        modulate_ofdm(np.zeros(0, np.complex64))
    with pytest.raises(OfdmError):
        modulate_ofdm(np.ones(5, np.complex64), pilot_version=2)
    with pytest.raises(OfdmError):
        modulate_ofdm(np.array([np.nan], np.complex64))
    assert modulate_ofdm(np.ones(5, np.complex64), OfdmNumerology()).ofdm_symbol_count == 1


@pytest.mark.parametrize("index", [8, 12, 16, 17])
def test_a_burst_decodes_to_the_same_body_either_way(backend, index):
    body = np.random.default_rng(index).integers(0, 256, 3000, dtype=np.uint8).tobytes()
    bursts = {}
    for choice in ("python", "native"):
        backend(choice)
        bursts[choice] = encode_burst_v5(body, mcs_entry(index), scrambler_seed=9)
    assert bursts["native"].header == bursts["python"].header
    assert np.max(np.abs(bursts["native"].samples - bursts["python"].samples)) <= 1e-6
    backend("python")
    for burst in bursts.values():
        samples = np.concatenate((np.zeros(100), burst.samples, np.zeros(200))).astype(np.complex64)
        settings = BurstConfig()
        found = acquire_all(samples, None, settings.v5.acquisition)
        decoded = _decode_burst_with_acquisition(
            samples, settings, found[0], frame_decoder=DEFAULT_DUAL_VERSION_FRAME_DECODER,
            symbol_demapper=demap_symbols, soft_symbol_demapper=None, input_noise_variance=None)
        assert decoded.body == body and decoded.fcs_ok

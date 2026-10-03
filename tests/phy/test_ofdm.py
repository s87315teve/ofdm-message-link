from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.codec import MCS, map_symbols
from ofdm_link.phy.ofdm import (
    OfdmError,
    OfdmNumerology,
    demodulate_ofdm,
    modulate_ofdm,
    pilot_symbols,
)


def test_default_numerology_has_explicit_80211a_like_allocation() -> None:
    numerology = OfdmNumerology()

    assert numerology.fft_size == 64
    assert numerology.cp_length == 16
    assert len(numerology.data_carriers) == 48
    assert numerology.pilot_carriers == (-21, -7, 7, 21)
    assert numerology.dc_carrier == 0
    assert numerology.guard_carriers == (
        -32,
        -31,
        -30,
        -29,
        -28,
        -27,
        27,
        28,
        29,
        30,
        31,
    )
    assert set(numerology.data_carriers).isdisjoint(numerology.pilot_carriers)

    with pytest.raises(FrozenInstanceError):
        numerology.cp_length = 8  # type: ignore[misc]


def test_pilot_polarity_v1_is_a_versioned_known_vector() -> None:
    pilots = pilot_symbols(5, first_symbol_index=0, version=1)

    expected = np.array(
        [
            [1, 1, 1, -1],
            [1, 1, 1, -1],
            [1, 1, 1, -1],
            [-1, -1, -1, 1],
            [-1, -1, -1, 1],
        ],
        dtype=np.complex64,
    )
    np.testing.assert_array_equal(pilots, expected)

    with pytest.raises(OfdmError, match="pilot polarity version"):
        pilot_symbols(1, version=2)


def test_aligned_modulation_round_trip_reports_padding() -> None:
    payload = np.array(
        [complex(index / 10, -(index + 1) / 20) for index in range(50)],
        dtype=np.complex64,
    )

    waveform = modulate_ofdm(payload)
    recovered = demodulate_ofdm(
        waveform.samples,
        payload_symbol_count=waveform.payload_symbol_count,
    )

    assert waveform.ofdm_symbol_count == 2
    assert waveform.padding_symbol_count == 46
    assert waveform.samples.shape == (160,)
    assert not waveform.samples.flags.writeable
    np.testing.assert_allclose(recovered, payload, rtol=1e-6, atol=1e-6)


def test_resource_grid_has_known_pilots_nulls_cp_and_unitary_energy() -> None:
    numerology = OfdmNumerology()
    payload = np.ones(48, dtype=np.complex64) * (2 + 1j)

    waveform = modulate_ofdm(payload, first_symbol_index=3)
    block = waveform.samples.reshape(1, 80)[0]
    useful = block[16:]
    grid = np.fft.fft(useful, norm="ortho")

    np.testing.assert_array_equal(block[:16], useful[-16:])
    assert useful[0] == pytest.approx(11.75 + 6j, rel=1e-6)
    np.testing.assert_allclose(
        grid[np.asarray(numerology.data_carriers) % 64], payload, rtol=1e-6, atol=1e-6
    )
    np.testing.assert_allclose(
        grid[np.asarray(numerology.pilot_carriers) % 64],
        np.array([-1, -1, -1, 1], dtype=np.complex64),
        rtol=1e-6,
        atol=1e-6,
    )
    np.testing.assert_allclose(
        grid[np.asarray((numerology.dc_carrier, *numerology.guard_carriers)) % 64],
        0,
        atol=1e-6,
    )
    assert np.sum(np.abs(useful) ** 2) == pytest.approx(48 * 5 + 4, rel=1e-6)


@pytest.mark.parametrize("mcs", list(MCS))
def test_qpsk_and_qam16_mapped_symbols_round_trip(mcs: MCS) -> None:
    bit_count = 49 * mcs.bits_per_symbol
    bits = (np.arange(bit_count) % 2).astype(np.uint8)
    payload = map_symbols(bits, mcs)

    waveform = modulate_ofdm(payload)
    recovered = demodulate_ofdm(
        waveform.samples,
        payload_symbol_count=payload.size,
    )

    np.testing.assert_allclose(recovered, payload, rtol=1e-6, atol=1e-6)


def test_one_tap_equalization_recovers_payload_from_known_channel() -> None:
    numerology = OfdmNumerology()
    payload = np.exp(1j * np.arange(73, dtype=np.float32) / 5).astype(np.complex64)
    waveform = modulate_ofdm(payload)
    channel = (
        np.linspace(0.5, 1.7, len(numerology.active_carriers), dtype=np.float32)
        * np.exp(1j * np.linspace(-0.7, 0.9, len(numerology.active_carriers)))
    ).astype(np.complex64)

    blocks = waveform.samples.reshape(waveform.ofdm_symbol_count, 80)
    grid = np.fft.fft(blocks[:, 16:], axis=1, norm="ortho")
    active_bins = np.asarray(numerology.active_carriers) % 64
    grid[:, active_bins] *= channel
    faded = np.fft.ifft(grid, axis=1, norm="ortho")
    received = np.concatenate((faded[:, -16:], faded), axis=1).reshape(-1)

    recovered = demodulate_ofdm(
        received,
        payload_symbol_count=payload.size,
        channel_estimate=channel,
    )

    np.testing.assert_allclose(recovered, payload, rtol=1e-5, atol=1e-5)


@pytest.mark.parametrize("invalid_value", [0j, 1e-14 + 0j, complex(np.nan, 0)])
def test_equalizer_rejects_unusable_active_carrier_estimates(
    invalid_value: complex,
) -> None:
    numerology = OfdmNumerology()
    waveform = modulate_ofdm(np.ones(48, dtype=np.complex64))
    channel = np.ones(len(numerology.active_carriers), dtype=np.complex64)
    channel[11] = invalid_value

    with pytest.raises(OfdmError, match="channel_estimate"):
        demodulate_ofdm(
            waveform.samples,
            payload_symbol_count=48,
            channel_estimate=channel,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fft_size": 63}, "fft_size"),
        ({"cp_length": 64}, "cp_length"),
        ({"data_carriers": (0, *OfdmNumerology().data_carriers[1:])}, "DC carrier"),
        ({"pilot_carriers": (-21, -7, 7, 7)}, "unique and disjoint"),
    ],
)
def test_numerology_rejects_invalid_allocations(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(OfdmError, match=message):
        OfdmNumerology(**changes)  # type: ignore[arg-type]


def test_modulator_and_demodulator_reject_malformed_vectors() -> None:
    with pytest.raises(OfdmError, match="must not be empty"):
        modulate_ofdm(np.array([], dtype=np.complex64))
    with pytest.raises(OfdmError, match="one-dimensional"):
        modulate_ofdm(np.zeros((2, 2), dtype=np.complex64))
    with pytest.raises(OfdmError, match="finite"):
        modulate_ofdm(np.array([complex(np.inf, 0)], dtype=np.complex64))
    with pytest.raises(OfdmError, match="positive multiple of 80"):
        demodulate_ofdm(np.ones(79), payload_symbol_count=1)

    waveform = modulate_ofdm(np.ones(48, dtype=np.complex64))
    with pytest.raises(OfdmError, match="one value per active carrier"):
        demodulate_ofdm(
            waveform.samples,
            payload_symbol_count=48,
            channel_estimate=np.ones(51),
        )

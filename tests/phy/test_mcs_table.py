from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    decode_burst,
    encode_burst,
)
from ofdm_link.phy.codec import (
    SUPPORTED_FRAME_WIRE_VERSIONS,
    coded_frame_bit_count_by_wire_version,
    encode_frame,
    encode_frame_by_wire_version,
    make_dual_version_frame_decoder,
)
from ofdm_link.phy.mcs_table import (
    ALL_MCS_ENTRIES,
    MAX_MCS_INDEX,
    MCS_TABLE,
    V5_MCS_TABLE,
    V5_WIRE_VERSION,
    McsTableError,
    describe_mcs_entry,
    mcs_entry,
    mcs_entry_for_wire,
    mcs_index_for,
    mcs_index_for_wire,
    v5_entries,
    v5_payload_geometry,
)

_PAYLOAD = bytes(range(96))


def _frame(mcs: MCS, payload: bytes = _PAYLOAD) -> Frame:
    return Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, 7, payload)


def test_table_is_grouped_by_modulation_and_ordered_by_spectral_efficiency() -> None:
    assert [entry.index for entry in MCS_TABLE] == list(range(len(MCS_TABLE)))
    for modulation in (MCS.QPSK, MCS.QAM16):
        group = [entry for entry in MCS_TABLE if entry.modulation is modulation]
        efficiencies = [entry.spectral_efficiency for entry in group]
        assert efficiencies == sorted(efficiencies)
        assert [entry.index for entry in group] == sorted(entry.index for entry in group)


def test_every_entry_names_one_defined_wire_version() -> None:
    for entry in MCS_TABLE:
        assert entry.wire_version in SUPPORTED_FRAME_WIRE_VERSIONS
        assert mcs_entry(entry.index) is entry
        assert mcs_index_for(entry.modulation, entry.fec_scheme, entry.code_rate) == entry.index
        assert mcs_entry_for_wire(entry.modulation, entry.wire_version) is entry
        assert mcs_index_for_wire(entry.modulation, entry.wire_version) == entry.index


def test_modulation_and_wire_version_together_address_exactly_one_entry() -> None:
    """The header already carries both fields, so the pair must be unique."""

    keys = {(entry.modulation, entry.wire_version) for entry in MCS_TABLE}
    assert len(keys) == len(MCS_TABLE)


@pytest.mark.parametrize("entry", MCS_TABLE, ids=lambda entry: f"mcs{entry.index}")
def test_entry_coded_length_matches_the_encoder_it_names(entry) -> None:
    frame = _frame(entry.modulation)
    information_bits = (11 + len(_PAYLOAD)) * 8

    assert entry.coded_bit_count(information_bits) == coded_frame_bit_count_by_wire_version(
        frame, entry.wire_version
    )
    assert (
        encode_frame_by_wire_version(frame, entry.wire_version).size
        == entry.coded_bit_count(information_bits)
    )


@pytest.mark.parametrize("entry", MCS_TABLE, ids=lambda entry: f"mcs{entry.index}")
def test_every_entry_round_trips_through_a_complete_burst(entry) -> None:
    frame = _frame(entry.modulation)
    decoder = make_dual_version_frame_decoder(accept_all_wire_versions=True)

    burst = encode_burst(frame, wire_version=entry.wire_version)
    padded = np.concatenate(
        [
            np.zeros(37, dtype=np.complex64),
            burst.samples,
            np.zeros(37, dtype=np.complex64),
        ]
    )
    decoded = decode_burst(padded, frame_decoder=decoder)

    assert decoded.frame == frame
    assert decoded.diagnostics.wire_version == entry.wire_version


def test_higher_spectral_efficiency_sends_a_shorter_burst() -> None:
    """The table's ordering must show up as real airtime, not only as a label."""

    for modulation in (MCS.QPSK, MCS.QAM16):
        group = [entry for entry in MCS_TABLE if entry.modulation is modulation]
        lengths = [
            encode_burst(
                _frame(modulation),
                wire_version=entry.wire_version,
            ).samples.size
            for entry in group
        ]
        assert lengths == sorted(lengths, reverse=True)


def test_rate_one_half_entries_keep_the_stable_v1_wire_bits() -> None:
    """Adding rates must not disturb the frozen wire-v1 encoding."""

    for entry in MCS_TABLE:
        if entry.fec_scheme != "convolutional" or entry.code_rate != "1/2":
            continue
        frame = _frame(entry.modulation)
        np.testing.assert_array_equal(
            encode_frame_by_wire_version(frame, entry.wire_version),
            encode_frame(frame),
        )


def test_unknown_index_and_format_fail_closed() -> None:
    with pytest.raises(McsTableError, match="unknown mcs_index"):
        mcs_entry(MAX_MCS_INDEX + 1)
    with pytest.raises(McsTableError, match="mcs_index must be an integer"):
        mcs_entry("0")
    with pytest.raises(McsTableError, match="no table entry"):
        mcs_index_for(MCS.QPSK, "turbo", "1/2")
    with pytest.raises(McsTableError, match="no table entry"):
        mcs_entry_for_wire(MCS.QPSK, 9)


def test_description_names_the_index_modulation_and_rate() -> None:
    label = describe_mcs_entry(mcs_entry(5))

    assert "MCS 5" in label
    assert "16QAM" in label
    assert "r1/3" in label


# -- wire v5 entries -------------------------------------------------------


def test_legacy_entries_are_frozen() -> None:
    """Indices 0-7 stay exactly what the v1-v4 wire and telemetry already use."""

    assert [
        (entry.index, entry.modulation, entry.fec_scheme, entry.code_rate, entry.wire_version)
        for entry in MCS_TABLE
    ] == [
        (0, MCS.QPSK, "turbo", "1/3", 2),
        (1, MCS.QPSK, "convolutional", "1/3", 3),
        (2, MCS.QPSK, "convolutional", "1/2", 1),
        (3, MCS.QPSK, "uncoded", "1", 4),
        (4, MCS.QAM16, "turbo", "1/3", 2),
        (5, MCS.QAM16, "convolutional", "1/3", 3),
        (6, MCS.QAM16, "convolutional", "1/2", 1),
        (7, MCS.QAM16, "uncoded", "1", 4),
    ]
    assert all(entry.family == "legacy" and entry.tx_visible for entry in MCS_TABLE)
    assert all(entry.rate == Fraction(1, entry.code_rate_inverse) for entry in MCS_TABLE)


def test_v5_entries_have_the_designed_fields_and_spectral_efficiency() -> None:
    assert [
        (entry.index, entry.modulation, entry.code_rate, entry.spectral_efficiency)
        for entry in V5_MCS_TABLE
    ] == [
        (8, MCS.QPSK, "1/3", pytest.approx(2 / 3)),
        (9, MCS.QPSK, "1/2", 1.0),
        (10, MCS.QPSK, "2/3", pytest.approx(4 / 3)),
        (11, MCS.QAM16, "1/3", pytest.approx(4 / 3)),
        (12, MCS.QAM16, "1/2", 2.0),
        (13, MCS.QAM16, "2/3", pytest.approx(8 / 3)),
        (14, MCS.QAM16, "3/4", 3.0),
        (15, MCS.QAM64, "1/2", 3.0),
        (16, MCS.QAM64, "2/3", 4.0),
        (17, MCS.QAM64, "3/4", 4.5),
    ]
    for entry in V5_MCS_TABLE:
        assert entry.family == "v5"
        assert entry.fec_scheme == "turbo_rm"
        assert entry.wire_version == V5_WIRE_VERSION
        assert mcs_entry(entry.index) is entry
        assert mcs_index_for(entry.modulation, "turbo_rm", entry.code_rate) == entry.index
    assert ALL_MCS_ENTRIES == MCS_TABLE + V5_MCS_TABLE
    assert [entry.index for entry in ALL_MCS_ENTRIES] == list(range(MAX_MCS_INDEX + 1))


def test_v5_entries_are_ordered_within_each_modulation_group() -> None:
    for modulation in MCS:
        group = [entry for entry in V5_MCS_TABLE if entry.modulation is modulation]
        assert [entry.rate for entry in group] == sorted(entry.rate for entry in group)
        assert len({entry.rate for entry in group}) == len(group)


def test_hidden_entries_are_decodable_but_not_offered() -> None:
    assert [entry.index for entry in v5_entries()] == [8, 9, 10, 12, 13, 14, 15, 16, 17]
    assert v5_entries(include_hidden=True) == V5_MCS_TABLE
    assert not mcs_entry(11).tx_visible


def test_v5_entries_have_no_legacy_wire_identity() -> None:
    """Several v5 entries share a modulation, so the pair lookups stay legacy-only."""

    with pytest.raises(McsTableError, match="no table entry"):
        mcs_entry_for_wire(MCS.QPSK, V5_WIRE_VERSION)
    for entry in MCS_TABLE:
        assert mcs_entry_for_wire(entry.modulation, entry.wire_version) is entry


def test_rate_strings_parse_strictly() -> None:
    assert mcs_index_for(MCS.QAM16, "turbo_rm", "3/4") == 14
    assert mcs_index_for(MCS.QPSK, "uncoded", "1") == 3
    for spelling in ("2/4", "6/8", "0.5", "1/", "/2", "a/b", "1/0", " 1/2"):
        with pytest.raises(McsTableError, match="unsupported code_rate"):
            mcs_index_for(MCS.QPSK, "turbo_rm", spelling)
    with pytest.raises(McsTableError, match="no table entry"):
        mcs_index_for(MCS.QPSK, "turbo_rm", "3/4")


@pytest.mark.parametrize("entry", V5_MCS_TABLE, ids=lambda entry: f"mcs{entry.index}")
@pytest.mark.parametrize("psdu_bytes", [4, 19, 100, 947, 7555, 16384, 65535])
def test_v5_geometry_matches_the_float_reference(entry, psdu_bytes: int) -> None:
    geometry = v5_payload_geometry(entry, psdu_bytes)
    bits = 8 * psdu_bytes
    symbols = math.ceil(bits / (float(entry.rate) * 48 * entry.bits_per_symbol) - 1e-12)

    assert geometry.information_bits == bits
    assert geometry.ofdm_symbol_count == symbols
    assert geometry.available_bits == 48 * entry.bits_per_symbol * symbols
    assert geometry.available_bits == entry.coded_bit_count(bits)
    assert geometry.effective_rate <= entry.rate
    # One symbol fewer would exceed the nominal rate.
    assert bits > float(entry.rate) * 48 * entry.bits_per_symbol * (symbols - 1)
    assert geometry.rate_match.available_bits == geometry.available_bits
    assert geometry.rate_match.modulation_order == entry.bits_per_symbol


def test_v5_geometry_reproduces_the_design_example() -> None:
    """900-byte datagram in one subframe: L = 947, B = 7576, two code blocks."""

    low = v5_payload_geometry(mcs_entry(8), 947)
    high = v5_payload_geometry(mcs_entry(14), 947)

    assert low.rate_match.segmentation.block_count == 2
    assert (low.ofdm_symbol_count, round(low.effective_rate, 3)) == (237, 0.333)
    assert (high.ofdm_symbol_count, round(high.effective_rate, 3)) == (53, 0.744)


def test_v5_geometry_rejects_legacy_entries_and_bad_lengths() -> None:
    with pytest.raises(McsTableError, match="wire-v5"):
        v5_payload_geometry(mcs_entry(0), 100)
    for bad in (3, 65536, 100.0):
        with pytest.raises(McsTableError, match="psdu_bytes"):
            v5_payload_geometry(mcs_entry(8), bad)


def test_v5_description_names_64qam() -> None:
    assert describe_mcs_entry(mcs_entry(17)) == "MCS 17 — 64QAM, Turbo r3/4 (4.50 b/sym)"
    assert describe_mcs_entry(mcs_entry(10)) == "MCS 10 — QPSK, Turbo r2/3 (1.33 b/sym)"

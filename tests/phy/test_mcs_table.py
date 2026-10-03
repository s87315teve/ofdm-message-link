from __future__ import annotations

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
    MCS_TABLE,
    McsTableError,
    describe_mcs_entry,
    mcs_entry,
    mcs_entry_for_wire,
    mcs_index_for,
    mcs_index_for_wire,
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
        mcs_entry(len(MCS_TABLE))
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

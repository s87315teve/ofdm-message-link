from __future__ import annotations

from itertools import product

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    NativeSoftFecDecoderPool,
    decode_burst,
    encode_burst,
    make_soft_convolutional_frame_codec,
    soft_demap_symbols,
)
from ofdm_link.radio import SimulationChannelConfig, apply_simulation_channel


@pytest.mark.gnuradio
@pytest.mark.parametrize("mcs,snr_db", [(MCS.QPSK, 15.0), (MCS.QAM16, 24.0)])
def test_native_soft_complete_burst_nominal_impairment_matrix(
    mcs: MCS,
    snr_db: float,
) -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        mcs,
        0x2468,
        bytes((index * 31 + 9) & 0xFF for index in range(96)),
    )
    transmitted = encode_burst(frame)

    with NativeSoftFecDecoderPool(max_lengths=2) as pool:
        codec = make_soft_convolutional_frame_codec(soft_decoder=pool)
        for case_index, (cfo, sfo, amplitude) in enumerate(
            product((-0.137, 0.137), (-20.0, 0.0, 20.0), (0.5, 1.0, 2.0))
        ):
            received, channel = apply_simulation_channel(
                transmitted.samples,
                SimulationChannelConfig(
                    amplitude_gain=amplitude,
                    taps=(1 + 0j, 0.18 + 0.08j, 0.06 - 0.04j),
                    cfo_subcarriers=cfo,
                    sample_rate_offset_ppm=sfo,
                    snr_db=snr_db,
                    seed=4_000 + case_index,
                ),
            )

            decoded = decode_burst(
                received,
                frame_decoder=codec.decode,
                soft_symbol_demapper=soft_demap_symbols,
                input_noise_variance=channel.noise_power,
            )

            assert decoded.frame == frame
            assert decoded.diagnostics.decision_mode == "soft"
            assert decoded.diagnostics.estimated_noise_variance is not None
            assert np.isfinite(decoded.diagnostics.estimated_noise_variance)

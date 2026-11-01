"""Regenerate ``data.js`` for the guide page from the example's own encoder.

Run from the repository root (no radio needed):

    python -m guide.build_data

For every wire-v5 MCS the transmit menu offers it records, per payload size,
the payload OFDM symbols of a burst that carries one datagram, plus the
constants the page needs to size an aggregated burst.  It checks the page's
formula against the encoder, so the page never re-derives FEC block sizes on
its own.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ofdm_link.phy.ampdu import DELIMITER_BYTES, ampdu_body_length
from ofdm_link.phy.burst_v5 import PSDU_FCS_BYTES, V5_MAX_TRACKED_BURST_SAMPLES
from ofdm_link.phy.mcs_table import _v5_symbol_count, describe_mcs_entry, v5_entries
from ofdm_link.phy.sync_v5 import V5_LTF_LENGTH, V5_SIG_LENGTH, V5_STF_LENGTH
from ofdm_message_link import datagram, options
from ofdm_message_link.dashboard import MARGIN_DB, SNR_THRESHOLD_DB
from ofdm_message_link.link import FRAME_OVERHEAD_BYTES, MessageTransmitter

DATA_CARRIERS = 48
# Sizes also checked against a real encoded burst (cheap enough to run each time).
ENCODER_CHECK_SIZES = (1, 500, 968)


def _psdu_bytes(fragment_bytes: int, count: int = 1) -> int:
    """The page's formula for a PSDU of ``count`` equal fragments."""

    mpdu = FRAME_OVERHEAD_BYTES + datagram.HEADER_SIZE + fragment_bytes
    subframe = DELIMITER_BYTES + mpdu
    aligned = -(-subframe // 4) * 4
    return (count - 1) * aligned + subframe + PSDU_FCS_BYTES


def _page_symbols(psdu_bytes: int, bits_per_symbol: int, num: int, den: int) -> int:
    """The page's formula for payload OFDM symbols (no pad bits in v5)."""

    return max(1, -(-(8 * psdu_bytes * den) // (num * DATA_CARRIERS * bits_per_symbol)))


def main() -> None:
    table = []
    resolved = None
    for entry in v5_entries():
        parser = argparse.ArgumentParser()
        options.add_common_arguments(parser)
        options.add_aggregation_arguments(parser)
        resolved = options.resolve(parser.parse_args(["--mcs-index", str(entry.index)]))
        profile = resolved.profile
        max_payload = profile.max_message_payload_bytes
        guards = profile.guard_pre_samples + profile.guard_post_samples
        transmitter = MessageTransmitter(profile, mcs_entry=resolved.mcs_entry)
        num, den = entry.rate.numerator, entry.rate.denominator

        symbols = []
        for size in range(1, max_payload + 1):
            mpdu = FRAME_OVERHEAD_BYTES + datagram.HEADER_SIZE + size
            psdu = ampdu_body_length([mpdu]) + PSDU_FCS_BYTES
            assert psdu == _psdu_bytes(size), (entry.index, size)
            count = _v5_symbol_count(entry, 8 * psdu)
            assert count == _page_symbols(psdu, entry.bits_per_symbol, num, den)
            symbols.append(count)
        for size in ENCODER_CHECK_SIZES:
            (burst,) = transmitter.encode_message(bytes(size))
            fixed = V5_STF_LENGTH + V5_LTF_LENGTH + V5_SIG_LENGTH
            assert burst.sample_count == guards + fixed + 80 * symbols[size - 1], (
                entry.index,
                size,
                burst.sample_count,
            )
        # Aggregates of several equal fragments use the same formula.
        for count in (2, 5, 16):
            mpdu = FRAME_OVERHEAD_BYTES + datagram.HEADER_SIZE + 300
            assert ampdu_body_length([mpdu] * count) + PSDU_FCS_BYTES == _psdu_bytes(300, count)

        # Run-length encode: [first payload size, payload symbols] per step.
        steps = [[1, symbols[0]]]
        for size, count in enumerate(symbols[1:], start=2):
            if count != steps[-1][1]:
                steps.append([size, count])
        table.append(
            {
                "index": entry.index,
                "label": describe_mcs_entry(entry),
                "bitsPerSymbol": entry.bits_per_symbol,
                "rateNum": num,
                "rateDen": den,
                "snrThresholdDb": SNR_THRESHOLD_DB.get(entry.index),
                "steps": steps,
            }
        )
    assert resolved is not None
    phy = resolved.config.phy
    limits = resolved.aggregation
    profile = resolved.profile
    data = {
        "sampleRate": float(profile.sample_rate),
        "fftLen": phy.fft_len,
        "cpLen": phy.cyclic_prefix_len,
        "guardPreSamples": profile.guard_pre_samples,
        "guardPostSamples": profile.guard_post_samples,
        "stfSamples": V5_STF_LENGTH,
        "ltfSamples": V5_LTF_LENGTH,
        "sigSamples": V5_SIG_LENGTH,
        "datagramHeaderBytes": datagram.HEADER_SIZE,
        "frameOverheadBytes": FRAME_OVERHEAD_BYTES,
        "delimiterBytes": DELIMITER_BYTES,
        "fcsBytes": PSDU_FCS_BYTES,
        "dataCarriers": DATA_CARRIERS,
        "maxFragmentBytes": profile.max_message_payload_bytes,
        "aggregation": {
            "enabled": limits.enabled,
            "maxSubframes": limits.max_subframes,
            "maxPsduBytes": limits.max_psdu_bytes,
            "maxBurstAirtimeMs": limits.max_burst_airtime_s * 1e3,
            "maxBurstSamples": V5_MAX_TRACKED_BURST_SAMPLES,
        },
        "marginDb": MARGIN_DB,
        "mcs": table,
    }
    out = Path(__file__).with_name("data.js")
    out.write_text(
        "// Generated by build_data.py from the example's encoder. Do not edit.\n"
        "window.GUIDE_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

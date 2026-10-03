"""Burst airtime and TX service rate of one datagram per MCS, from the example's own encoder.

Run from the repository root (no radio needed):

    python -m experiments.ota_udp_throughput.airtime [SIZE]

The service-rate columns add the 250 us settle guard and the 20 us gap that
``UhdSampleSink`` puts around every burst (``transport.UhdSinkLimits``); that
is the ceiling a saturated transmitter actually reaches.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ofdm_link.phy.mcs_table import MCS_TABLE, describe_mcs_entry
from ofdm_message_link import options
from ofdm_message_link.link import MessageTransmitter
from ofdm_message_link.transport import UhdSinkLimits


def main() -> None:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 968
    limits = UhdSinkLimits()
    for entry in MCS_TABLE:
        parser = argparse.ArgumentParser()
        options.add_common_arguments(parser)
        resolved = options.resolve(
            parser.parse_args(["--transport", "uhd", "--mcs-index", str(entry.index)])
        )
        transmitter = MessageTransmitter(resolved.profile, mcs_entry=resolved.mcs_entry)
        bursts = transmitter.encode_message(bytes(size))
        samples = sum(burst.sample_count for burst in bursts)
        repeats = 50
        started = time.perf_counter()
        for _ in range(repeats):
            transmitter.encode_message(bytes(size))
        encode_s = (time.perf_counter() - started) / repeats

        fs = float(resolved.config.phy.sample_rate)
        per_burst_pad = round((limits.tx_settle_guard_seconds + limits.tx_burst_gap_seconds) * fs)
        padded = samples + len(bursts) * per_burst_pad
        print(
            json.dumps(
                {
                    "mcs": entry.index,
                    "label": describe_mcs_entry(entry),
                    "size": size,
                    "bursts": len(bursts),
                    "samples": samples,
                    "airtime_ms": samples / fs * 1e3,
                    "airtime_cap_per_s": fs / samples,
                    "airtime_cap_mbps": fs / samples * size * 8 / 1e6,
                    "service_per_s": fs / padded,
                    "service_mbps": fs / padded * size * 8 / 1e6,
                    "encode_ms": encode_s * 1e3,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

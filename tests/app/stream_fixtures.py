"""Fixed-seed sample streams for the end-to-end equivalence tests.

One stream is a run of v5 bursts from :class:`MessageTransmitter` (one
datagram each, several MCS), with idle gaps, AWGN, a CFO and optionally a DC
offset, so a receiver configuration or backend change can be judged by what
it delivers.
"""

from __future__ import annotations

import numpy as np

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    build_phy_profile,
    load_link_config,
)

CONFIG = "configs/default.yaml"
PROFILE = "configs/profiles/duplex_fdd_2420_2460.yaml"
MEASUREMENT = "configs/profiles/b210_measurement.yaml"


def profile(*overlays: str):
    return build_phy_profile(load_link_config(CONFIG, [PROFILE, *overlays]))


def v5_stream(
    profile,
    *,
    seed: int = 7,
    bursts: int = 12,
    mcs_indices: tuple[int, ...] = (8, 12, 16),
    payload_bytes: int = 300,
    snr_db: float = 18.0,
    cfo_subcarriers: float = 0.03,
    dc: complex = 0.0,
    gap_samples: tuple[int, int] = (200, 900),
) -> tuple[np.ndarray, list[bytes]]:
    """A noisy stream of v5 bursts and the payloads it carries, in order."""

    rng = np.random.default_rng(seed)
    pieces: list[np.ndarray] = [np.zeros(int(rng.integers(*gap_samples)), dtype=np.complex64)]
    payloads: list[bytes] = []
    for index in range(bursts):
        entry = mcs_entry(mcs_indices[index % len(mcs_indices)])
        transmitter = MessageTransmitter(profile, mcs_entry=entry)
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        for burst in transmitter.encode_message(payload):
            pieces.append(np.asarray(burst.samples, dtype=np.complex64))
        payloads.append(payload)
        pieces.append(np.zeros(int(rng.integers(*gap_samples)), dtype=np.complex64))
    stream = np.concatenate(pieces).astype(np.complex128)
    signal_power = float(np.mean(np.abs(stream[np.abs(stream) > 0]) ** 2))
    variance = signal_power / 10.0 ** (snr_db / 10.0)
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(stream.size, 2))
    stream = stream + noise[:, 0] + 1j * noise[:, 1]
    stream *= np.exp(2j * np.pi * cfo_subcarriers / 64.0 * np.arange(stream.size))
    stream += dc
    return stream.astype(np.complex64), payloads


def deliver(profile, stream: np.ndarray, *, chunk: int = 4096, workers: int = 1) -> list[bytes]:
    """Every payload a fresh receiver delivers from ``stream``, in order."""

    receiver = MessageReceiver(profile, decode_workers=workers)
    delivered = []
    try:
        for start in range(0, stream.size, chunk):
            delivered.extend(receiver.feed(stream[start : start + chunk]))
        delivered.extend(receiver.flush(30.0))
    finally:
        receiver.close()
    return [message.message.payload for message in delivered]

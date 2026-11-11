"""End to end, fixed seed: every native piece on and every piece forced to Python deliver alike.

The transmitter (rate matching, mapping, modulation) and the receiver
(acquisition, header probe, Turbo) each switch with OFDM_LINK_NATIVE, which
the decode workers inherit.
"""

from __future__ import annotations

import pytest
from stream_fixtures import MEASUREMENT, deliver, profile, v5_stream

from ofdm_link.phy import native_backend


@pytest.fixture
def backend(monkeypatch):
    def use(choice):
        for piece in native_backend.PIECES:
            monkeypatch.delenv(native_backend.env_name(piece), raising=False)
        monkeypatch.setenv(native_backend.GLOBAL_ENV, choice)
        native_backend.reset()

    yield use
    native_backend.reset()


@pytest.mark.parametrize("overlays", [(), (MEASUREMENT,)], ids=["legacy_on", "legacy_off"])
@pytest.mark.parametrize("snr_db", [8.0, 14.0, 25.0])
def test_native_and_python_deliver_the_same(backend, overlays, snr_db):
    link = profile(*overlays)
    delivered = {}
    for choice in ("python", "native"):
        backend(choice)
        stream, payloads = v5_stream(link, seed=int(snr_db) + 100, snr_db=snr_db,
                                     mcs_indices=(8, 10, 12, 13), dc=0.002)
        delivered[choice] = deliver(link, stream)
    assert delivered["native"] == delivered["python"]
    if snr_db >= 14.0:
        assert len(delivered["native"]) == len(payloads)


def test_native_and_python_deliver_the_same_with_decode_workers(backend):
    link = profile(MEASUREMENT)
    delivered = {}
    for choice in ("python", "native"):
        backend(choice)
        stream, payloads = v5_stream(link, seed=7, snr_db=20.0, bursts=10)
        delivered[choice] = deliver(link, stream, workers=2)
    assert delivered["native"] == delivered["python"]
    assert len(delivered["native"]) == len(payloads)

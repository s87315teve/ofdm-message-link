"""M1: how much a Pluto's own transmitter hurts its receiver at FDD spacing S.

The device under test (DUT) receives at ``f_rx`` while its own transmitter
sends real v5 MCS 14 aggregates (about 6.9 ms) at ``f_tx``.  The peer Pluto
sends known MCS 8 bursts (one 900-byte datagram, 100/s) at ``f_rx``.  The
DUT's receive and transmit share one libiio context
(:class:`~ofdm_message_link.pluto.SharedPlutoContext`): over USB a Pluto
opens in one context only.  Design: duplex design §1.

Per combination (spacing S, DUT, RX gain g, RX analog bandwidth b) the phases
are (2 s each; capture analysed right after, with both transmitters at the
minimum, raw IQ deleted unless --keep-raw):

    a          peer off, own TX not opened        ambient floor, Wi-Fi occupancy
    c-mid/edge peer at ~15/~10 dB SNR, own closed baseline SNR and loss
    b0         own TX opened at -5 dB, idle       LO leakage, idle TX noise
    b1..b4     own bursts, duty 0.9, -20/-10/-5/0 dB   noise rise vs own gain
    b5         own bursts, duty 0.3, -5 dB        duty dependence
    d-mid/edge peer on, own bursts duty 0.9 at -5 and 0 dB   SNR and loss with SI

plus, at S = 0 (both at 2440 MHz, own TX -20 dB, peer off), the own-burst
level, clipping, and the samples until the floor returns after a burst (for
the LBT fallback).

    PYTHONPATH=src python experiments/duplex_fdd/si_scan.py run \\
        --dut 104473541196000419000c00f2ff1e6e77 --peer 10447354119600041b000b00f2ff1ef9c6 \\
        --out experiments/duplex_fdd/results/si/run1 \\
        --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
    PYTHONPATH=src python experiments/duplex_fdd/si_scan.py summarize \\
        experiments/duplex_fdd/results/si/run1

``run`` swaps the roles and repeats unless --single-dut.  ``--dry-run`` does
everything without radios (a synthetic capture per phase), for testing the
pipeline.  ``summarize`` writes ``si_scan.json`` (every phase record plus the
per-combination metrics and the decision) and ``si_summary.md``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hw_common  # noqa: E402
from hw_common import Pacer  # noqa: E402

from ofdm_link.phy.ofdm import OfdmNumerology  # noqa: E402

SAMPLE_RATE = 5_000_000
CENTRE_HZ = 2_440_000_000
OWN_GAINS_DB = (-20.0, -10.0, -5.0, 0.0)
PLANNED_TX_GAIN_DB = -5.0
MIN_TX_GAIN_DB = -89.75
PEER_RATE_PER_S = 100.0
PEER_PAYLOAD_BYTES = 900
OWN_MCS = 14
PEER_MCS = 8
OWN_BURST_TARGET_S = 0.0069
SNR_TARGETS_DB = {"mid": 15.0, "edge": 10.0}
CLIP_LSB = 1900
RETUNE_WAIT_S = 0.2
TX_TUNE_WAIT_S = 0.1
# The S = 0 own-burst measurement.
LBT_OWN_GAIN_DB = -20.0
LBT_DUTY = 0.3
# Decision thresholds, design §1.3.
PASS = {"dn_db": 1.0, "dsnr_db": 1.0, "dloss_pp": 0.5, "clip": 1e-6, "dovf_per_min": 0.5}
MARGINAL = {"dsnr_db": 3.0, "dloss_pp": 2.0, "clip": 1e-4}
T14_DB = 17.5  # dashboard.SNR_THRESHOLD_DB[14] when this was written; read live below


@dataclass(frozen=True)
class Phase:
    name: str
    peer: str | None = None  # None, "mid" or "edge"
    own: str | None = None  # None (closed), "idle" or "bursts"
    own_gain_db: float | None = None
    duty: float = 0.0


PHASES: tuple[Phase, ...] = (
    Phase("a"),
    Phase("c-mid", peer="mid"),
    Phase("c-edge", peer="edge"),
    Phase("b0", own="idle", own_gain_db=PLANNED_TX_GAIN_DB),
    *(
        Phase(f"b{index}", own="bursts", own_gain_db=gain, duty=0.9)
        for index, gain in enumerate(OWN_GAINS_DB, start=1)
    ),
    Phase("b5", own="bursts", own_gain_db=PLANNED_TX_GAIN_DB, duty=0.3),
    Phase("d-mid-m5", peer="mid", own="bursts", own_gain_db=-5.0, duty=0.9),
    Phase("d-mid-0", peer="mid", own="bursts", own_gain_db=0.0, duty=0.9),
    Phase("d-edge-m5", peer="edge", own="bursts", own_gain_db=-5.0, duty=0.9),
    Phase("d-edge-0", peer="edge", own="bursts", own_gain_db=0.0, duty=0.9),
)
LBT_PHASES: tuple[Phase, ...] = (
    Phase("a"),
    Phase("lbt-own", own="bursts", own_gain_db=LBT_OWN_GAIN_DB, duty=LBT_DUTY),
)


def frequencies(spacing_hz: float, dut_is_node_a: bool) -> tuple[float, float]:
    """(f_rx, f_tx) of the DUT: node A transmits on the lower frequency."""

    low, high = CENTRE_HZ - spacing_hz / 2, CENTRE_HZ + spacing_hz / 2
    return (high, low) if dut_is_node_a else (low, high)


# --------------------------------------------------------------------------
# analysis (pure; tested without radios)
# --------------------------------------------------------------------------


def _db(value: float) -> float:
    return 10.0 * math.log10(value) if value > 0 else -300.0


def _bins() -> tuple[np.ndarray, np.ndarray]:
    numerology = OfdmNumerology()
    active = np.asarray(numerology.active_carriers) % numerology.fft_size
    guard = np.setdiff1d(np.arange(1, numerology.fft_size), active)
    return active, guard


def noise_floor(samples: np.ndarray, *, mask_bursts: bool) -> dict[str, object]:
    """Mean power per bin over 64-point ortho FFT blocks, in dBFS (full scale 2048)."""

    active, guard = _bins()
    blocks = samples[: samples.size // 64 * 64].reshape(-1, 64)
    if blocks.shape[0] == 0:
        return {"blocks": 0}
    power = np.abs(np.fft.fft(blocks, axis=1, norm="ortho")) ** 2
    in_band = power[:, active].mean(axis=1)
    keep = np.ones(blocks.shape[0], dtype=bool)
    if mask_bursts:
        keep = in_band <= np.median(in_band) * 10**0.3
    spectrum = power[keep].mean(axis=0)
    spectrum_db = [round(_db(float(value)), 2) for value in spectrum]
    data_db = np.asarray(spectrum_db)[active]
    guard_db = np.asarray(spectrum_db)[guard]
    spur_data = float(np.max(data_db) - np.median(data_db))
    spur_guard = float(np.max(guard_db) - np.median(guard_db))
    # Wi-Fi and other bursty users: 1-ms blocks above the floor.
    per_ms = 78  # 64-sample blocks per ms at 5 MS/s
    usable = in_band.size // per_ms * per_ms
    occupancy = None
    if usable:
        ms_power = in_band[:usable].reshape(-1, per_ms).mean(axis=1)
        floor = np.percentile(ms_power, 20)
        occupancy = float(np.mean(ms_power > floor * 10**0.6))
    return {
        "blocks": int(blocks.shape[0]),
        "blocks_kept": int(keep.sum()),
        "p_data_dbfs": round(_db(float(spectrum[active].mean())), 2),
        "p_guard_dbfs": round(_db(float(spectrum[guard].mean())), 2),
        "p_dc_dbfs": round(_db(float(spectrum[0])), 2),
        "spur_data_db": round(spur_data, 2),
        "spur_guard_db": round(spur_guard, 2),
        "spur_bin": int(np.argmax(np.where(np.arange(64) == 0, -1e9, spectrum_db))),
        "spectrum_dbfs": spectrum_db,
        "occupancy_1ms": occupancy,
    }


def saturation(samples: np.ndarray) -> dict[str, object]:
    raw = hw_common.to_int16(samples)
    if raw.size == 0:
        return {"max_abs_lsb": 0, "clip_fraction": 0.0, "rms_dbfs": -300.0}
    magnitude = np.abs(raw.astype(np.int32))
    clipped = np.maximum(magnitude[0::2], magnitude[1::2]) >= CLIP_LSB
    return {
        "max_abs_lsb": int(magnitude.max()),
        "clip_fraction": float(clipped.mean()),
        "rms_dbfs": round(_db(float(np.mean(np.abs(samples) ** 2))), 2),
    }


def burst_recovery(samples: np.ndarray, *, smooth: int = 32) -> dict[str, object]:
    """Own-burst level, and samples from each burst end until the floor returns."""

    if samples.size < smooth * 4:
        return {"bursts": 0}
    power = np.abs(samples) ** 2
    envelope = np.convolve(power, np.ones(smooth) / smooth, mode="valid")
    floor = float(np.percentile(envelope, 10))
    level = float(np.percentile(envelope, 90))
    high = level * 0.1  # level - 10 dB
    above = envelope > high
    ends = np.flatnonzero(above[:-1] & ~above[1:]) + 1
    recovery = []
    limit = int(0.005 * SAMPLE_RATE)
    quiet = envelope <= floor * 2.0  # floor + 3 dB
    for end in ends:
        window = quiet[end : end + limit]
        hits = np.flatnonzero(window)
        if hits.size:
            recovery.append(int(hits[0]))
    result: dict[str, object] = {
        "bursts": int(ends.size),
        "burst_level_dbfs": round(_db(level), 2),
        "floor_dbfs": round(_db(floor), 2),
        "smoothing_samples": smooth,
    }
    if recovery:
        values = np.asarray(recovery)
        result.update(
            recovery_samples_median=float(np.median(values)),
            recovery_samples_p90=float(np.percentile(values, 90)),
            recovery_samples_max=int(values.max()),
            recovery_ms_p90=round(float(np.percentile(values, 90)) / SAMPLE_RATE * 1e3, 3),
        )
    return result


def decode_peer(samples: np.ndarray, profile, sent_sequences: set[int]) -> dict[str, object]:
    """Offline decode with the app's receiver: preamble SNR, loss and failure counters."""

    from ofdm_message_link.link import MessageReceiver

    receiver = MessageReceiver(profile, decode_workers=1)
    received: set[int] = set()
    snrs: list[float] = []
    chunk = 65536
    for start in range(0, samples.size, chunk):
        for message in receiver.feed(samples[start : start + chunk]):
            received.add(int(message.sequence))
        snrs.extend(receiver.drain_link_snr_db())
    receiver.close()
    snapshot = receiver.decoder_snapshot()
    stats = receiver.stats
    got = len(received & sent_sequences)
    sent = len(sent_sequences)
    values = np.asarray(snrs, dtype=float)
    return {
        "peer_sent": sent,
        "peer_received": got,
        "loss": None if sent == 0 else round(1.0 - got / sent, 5),
        "snr_median_db": None if values.size == 0 else round(float(np.median(values)), 2),
        "snr_p10_db": None if values.size == 0 else round(float(np.percentile(values, 10)), 2),
        "preambles": int(values.size),
        "header_decode_failure": int(snapshot.header_decode_failure),
        "psdu_fcs_failures": int(snapshot.psdu_fcs_failures),
        "missing_bursts_tracker": int(stats.missing_bursts),
    }


def analyse(samples: np.ndarray, phase: Phase, profile, sent_sequences: set[int]) -> dict:
    result: dict[str, object] = {
        "noise": noise_floor(samples, mask_bursts=phase.peer is not None),
        "saturation": saturation(samples),
        "captured_samples": int(samples.size),
    }
    if phase.peer is not None:
        result["peer_rx"] = decode_peer(samples, profile, sent_sequences)
    if phase.name == "lbt-own":
        result["recovery"] = burst_recovery(samples)
    return result


# --------------------------------------------------------------------------
# radio side
# --------------------------------------------------------------------------


@dataclass
class Waveforms:
    own: list[np.ndarray]
    own_burst_samples: int
    own_subframes: int
    peer: list[np.ndarray]
    peer_sequences: list[int]
    peer_burst_samples: int
    profile: object = field(repr=False, default=None)


def build_waveforms(seconds: float) -> Waveforms:
    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
    from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

    config = load_link_config(DEFAULT_CONFIG, [DEFAULT_OVERLAY])
    profile = build_phy_profile(config, mcs_entry=mcs_entry(PEER_MCS))
    rng = np.random.default_rng(7)
    own_tx = MessageTransmitter(profile, mcs_entry=mcs_entry(OWN_MCS))
    datagrams = []
    while True:
        body = rng.integers(0, 256, PEER_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
        candidate = [*datagrams, *own_tx.fragment_message(body)]
        from ofdm_message_link.link import aggregate_body_length

        samples = own_tx.burst_sample_count(mcs_entry(OWN_MCS), aggregate_body_length(candidate))
        if samples > OWN_BURST_TARGET_S * SAMPLE_RATE and datagrams:
            break
        datagrams = candidate
    own = [own_tx.encode_datagrams(datagrams).samples for _ in range(8)]
    peer_tx = MessageTransmitter(profile, mcs_entry=mcs_entry(PEER_MCS))
    count = int(math.ceil(seconds * PEER_RATE_PER_S))
    peer_bursts = []
    sequences = []
    for index in range(count):
        body = (
            np.random.default_rng(1000 + index)
            .integers(0, 256, PEER_PAYLOAD_BYTES, dtype=np.uint8)
            .tobytes()
        )
        (burst,) = peer_tx.encode_message(body)
        peer_bursts.append(burst.samples)
        sequences.append(burst.sequence)
    return Waveforms(
        own=own,
        own_burst_samples=int(own[0].size),
        own_subframes=len(datagrams),
        peer=peer_bursts,
        peer_sequences=sequences,
        peer_burst_samples=int(peer_bursts[0].size),
        profile=profile,
    )


class _DryRadio:
    """Stands in for the radios with --dry-run: a synthetic noise capture."""

    def __init__(self) -> None:
        self.gain = MIN_TX_GAIN_DB
        self.rng = np.random.default_rng(3)

    def send(self, samples) -> bool:
        time.sleep(samples.size / SAMPLE_RATE)
        return True

    def set_gain(self, gain: float) -> float:
        self.gain = gain
        return gain


class Block:
    """One (spacing, DUT) block: the DUT's shared context with RX and TX, and the peer's TX."""

    def __init__(
        self,
        args,
        token,
        devices,
        dut: str,
        peer: str,
        f_rx: float,
        f_tx: float,
        rx_gain: float,
        rx_bw: float,
    ) -> None:
        self.args = args
        self.token = token
        self.dry = args.dry_run
        self.dut, self.peer = dut, peer
        self.f_rx, self.f_tx = f_rx, f_tx
        self.rx_gain, self.rx_bw = rx_gain, rx_bw
        self.devices = devices
        self.shared = None
        self.source = None
        self.sink = None
        self.peer_sink = None
        self.recorder = None
        self.startup_overflows = 0
        self.dry_radio = _DryRadio() if self.dry else None
        self.dry_peer = _DryRadio() if self.dry else None

    def open(self) -> None:
        if self.dry:
            return
        from ofdm_message_link import pluto

        dut = self.devices[self.dut]
        peer = self.devices[self.peer]
        self.shared = pluto.SharedPlutoContext(
            dut.address, self.dut, rx_kernel_buffers=self.args.rx_kernel_buffers
        )
        try:
            self.shared.configure(SAMPLE_RATE)
            self.source = pluto.PlutoSampleSource(
                pluto.PlutoSettings(
                    dut.address,
                    self.dut,
                    SAMPLE_RATE,
                    self.f_rx,
                    self.rx_gain,
                    rf_bandwidth=self.rx_bw,
                ),
                open_context=self.shared.factory(),
            )
            self.source.start()
            self.recorder = hw_common.Recorder(self.source)
            self.recorder.start()
            self.peer_sink = pluto.PlutoSampleSink(
                pluto.PlutoSettings(
                    peer.address, self.peer, SAMPLE_RATE, self.f_rx, MIN_TX_GAIN_DB
                ),
                self.token,
            )
            self.peer_sink.start()
            time.sleep(3.0)
            self.startup_overflows = int(self.source.snapshot()["rx_overflow_events"])
        except BaseException:
            self.close()
            raise

    def own_open(self, gain: float) -> None:
        if self.dry:
            self.dry_radio.set_gain(gain)
            return
        from ofdm_message_link import pluto

        if self.sink is None:
            self.sink = pluto.PlutoSampleSink(
                pluto.PlutoSettings(
                    self.devices[self.dut].address, self.dut, SAMPLE_RATE, self.f_tx, MIN_TX_GAIN_DB
                ),
                self.token,
                open_context=self.shared.factory(),
            )
            self.sink.start()
            time.sleep(TX_TUNE_WAIT_S)
        self.sink.set_gain(gain)

    def own_close(self) -> None:
        if self.sink is not None:
            self.sink.stop()
            self.sink = None

    def own_sink(self):
        return self.dry_radio if self.dry else self.sink

    def peer_out(self):
        return self.dry_peer if self.dry else self.peer_sink

    def quiet(self) -> None:
        """Both transmitters at the minimum (analysis runs in between phases)."""

        if self.dry:
            self.dry_peer.set_gain(MIN_TX_GAIN_DB)
            return
        if self.sink is not None:
            self.sink.set_gain(MIN_TX_GAIN_DB)
        if self.peer_sink is not None:
            self.peer_sink.set_gain(MIN_TX_GAIN_DB)

    def set_rx(self, gain: float, bandwidth: float) -> None:
        if self.dry:
            self.rx_gain, self.rx_bw = gain, bandwidth
            return
        if gain != self.rx_gain:
            self.source.set_gain(gain)
            self.rx_gain = gain
        if bandwidth != self.rx_bw:
            self.shared.write_attr(
                "ad9361-phy", "voltage0", "rf_bandwidth", str(round(bandwidth)), output=False
            )
            self.rx_bw = bandwidth
        time.sleep(RETUNE_WAIT_S)

    def overflows(self) -> int:
        return 0 if self.dry else int(self.source.snapshot()["rx_overflow_events"])

    def device_snapshot(self) -> dict:
        if self.dry:
            return {"dry_run": True}
        snapshot = self.shared.snapshot()
        snapshot["source"] = self.source.snapshot().get("readback")
        if self.sink is not None:
            snapshot["sink"] = self.sink.snapshot().get("readback")
        return snapshot

    def capture(self, seconds: float, peer: Pacer | None) -> tuple[np.ndarray, float | None]:
        """Record ``seconds`` (or until the peer finished); RSSI read halfway."""

        if self.dry:
            return self._dry_capture(seconds, peer), None
        self.recorder.take()
        self.recorder.armed.set()
        started = time.monotonic()
        if peer is not None:
            peer.start()
        time.sleep(seconds / 2)
        rssi = self.shared.snapshot().get("rssi_db")
        if peer is not None:
            peer.join(timeout=seconds * 3)
        remaining = seconds - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        time.sleep(0.15)
        self.recorder.armed.clear()
        return self.recorder.take(), rssi

    def _dry_capture(self, seconds: float, peer: Pacer | None) -> np.ndarray:
        """Noise, plus the peer's bursts at SNR = 27.5 dB + peer gain (the measured path)."""

        rng = self.dry_radio.rng
        n = int((seconds + 0.02) * SAMPLE_RATE)
        noise_power = 2 * 0.003**2
        samples = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.003
        if peer is not None:
            peer.start()
            peer.join()
            snr_db = 27.5 + self.dry_peer.gain
            for index in range(peer.sent):
                burst = peer.bursts[peer.sent_indices[index]]
                start = int(0.01 * SAMPLE_RATE) + int(index / PEER_RATE_PER_S * SAMPLE_RATE)
                if start + burst.size > n:
                    break
                power = np.mean(np.abs(burst) ** 2)
                scale = np.sqrt(noise_power * 10 ** (snr_db / 10) / power)
                samples[start : start + burst.size] += burst * scale
        return samples.astype(np.complex64)

    def close(self) -> None:
        for closer in (
            self.own_close,
            lambda: self.peer_sink and self.peer_sink.stop(),
            lambda: self.recorder and self.recorder.close(),
            lambda: self.source and self.source.stop(),
            lambda: self.shared and self.shared.close(),
        ):
            try:
                closer()
            except Exception as error:  # noqa: BLE001 - closing must go on
                print(f"close: {error}", file=sys.stderr)
        self.sink = self.peer_sink = self.recorder = self.source = None


def calibrate_peer(
    block: Block, waves: Waveforms, target_db: float, start_gain: float, seconds: float = 1.0
) -> dict:
    """Find the peer TX gain that gives ``target_db`` preamble SNR with own TX closed."""

    gain = start_gain
    history = []
    count = int(seconds * PEER_RATE_PER_S)
    sent = set(waves.peer_sequences[:count])
    for _ in range(5):
        block.peer_out().set_gain(gain)
        pacer = Pacer(block.peer_out(), waves.peer, rate=PEER_RATE_PER_S, count=count)
        samples, _ = block.capture(seconds, pacer)
        block.quiet()
        result = decode_peer(samples, waves.profile, sent)
        snr = result["snr_median_db"]
        history.append({"gain_db": gain, "snr_median_db": snr, "loss": result["loss"]})
        if snr is None:
            gain = min(0.0, gain + 10.0)
            continue
        if abs(snr - target_db) <= 1.0:
            break
        gain = float(np.clip(round((gain + target_db - snr) * 4) / 4, -60.0, 0.0))
        if history[-1]["gain_db"] == gain:
            break  # at the limit
    final = history[-1]
    return {
        "target_db": target_db,
        "gain_db": final["gain_db"],
        "snr_median_db": final["snr_median_db"],
        "within_1db": final["snr_median_db"] is not None
        and abs(final["snr_median_db"] - target_db) <= 1.0,
        "history": history,
    }


def run_phase(
    block: Block, phase: Phase, waves: Waveforms, peer_gains: dict, combo: dict, args, out: Path
) -> dict:
    if phase.own is None:
        block.own_close()
    else:
        block.own_open(phase.own_gain_db)
    own = None
    if phase.own == "bursts":
        own = Pacer(block.own_sink(), waves.own, duty=phase.duty)
    peer = None
    sent: set[int] = set()
    if phase.peer is not None:
        block.peer_out().set_gain(peer_gains[phase.peer])
        count = int(args.seconds * PEER_RATE_PER_S)
        peer = Pacer(block.peer_out(), waves.peer, rate=PEER_RATE_PER_S, count=count)
        sent = set(waves.peer_sequences[:count])
    if own is not None:
        own.start()
        time.sleep(0.1)
    load = hw_common.loadavg()
    before = block.overflows()
    started_unix = time.time()
    samples, rssi = block.capture(args.seconds, peer)
    after = block.overflows()
    if own is not None:
        own.finish()
    block.quiet()
    duration = samples.size / SAMPLE_RATE
    record = {
        "kind": "phase",
        **combo,
        "phase": phase.name,
        "peer_level": phase.peer,
        "peer_gain_db": None if phase.peer is None else peer_gains[phase.peer],
        "own_state": phase.own or "closed",
        "own_gain_db": phase.own_gain_db,
        "duty_requested": phase.duty,
        "duty_achieved": None
        if own is None or own.elapsed_s == 0
        else round(own.samples / SAMPLE_RATE / own.elapsed_s, 3),
        "own_bursts": None if own is None else own.sent,
        "own_error": None if own is None else own.error,
        "peer_error": None if peer is None else peer.error,
        "overflow_events": after - before,
        "overflows_per_min": round((after - before) / max(duration, 1e-9) * 60.0, 3),
        "rssi_db": rssi,
        "loadavg": load,
        "started_unix": started_unix,
        "duration_s": round(duration, 3),
    }
    record.update(analyse(samples, phase, waves.profile, sent))
    if args.keep_raw:
        raw_dir = out / "raw"
        raw_dir.mkdir(exist_ok=True)
        stem = f"{combo['combo_id']}_{phase.name}"
        np.save(raw_dir / f"{stem}.npy", hw_common.to_int16(samples))
    return record


def _combos(args) -> list[tuple[float, float, float]]:
    """(spacing, rx gain, rx bandwidth) in the order they run."""

    combos = []
    for spacing in args.spacings:
        for gain in args.rx_gains:
            combos.append((spacing, gain, args.bw))
        if spacing == 40e6:
            for bandwidth in args.extra_bw:
                combos.append((spacing, args.rx_gains[0], bandwidth))
    return combos


def run(args) -> int:
    token = None if args.dry_run else hw_common.acknowledge(args)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    records_path = out / "records.jsonl"
    meta = hw_common.base_meta(args)
    meta["design"] = "duplex design §1.2/§1.3"
    load_logger = hw_common.LoadLogger(out / "loadavg.log").start()
    roles = (
        [(args.dut, args.peer)]
        if args.single_dut
        else [(args.dut, args.peer), (args.peer, args.dut)]
    )
    try:
        with hw_common.hardware_lock() if not args.dry_run else _null():
            meta["loadavg_gate"] = (
                hw_common.loadavg() if args.dry_run else hw_common.wait_for_load(args.max_load)
            )
            waves = build_waveforms(args.seconds)
            meta["own_burst_samples"] = waves.own_burst_samples
            meta["own_burst_ms"] = waves.own_burst_samples / SAMPLE_RATE * 1e3
            meta["own_subframes"] = waves.own_subframes
            meta["peer_burst_samples"] = waves.peer_burst_samples
            (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
            devices = (
                {}
                if args.dry_run
                else hw_common.with_retries(
                    "discover", lambda: hw_common.find_plutos((args.dut, args.peer))
                )[0]
            )
            for dut, peer in roles:
                dut_is_a = dut == args.node_a
                by_spacing: dict[float, list[tuple[float, float, float]]] = {}
                for combo in _combos(args):
                    by_spacing.setdefault(combo[0], []).append(combo)
                for spacing, combos in by_spacing.items():
                    run_block(
                        args,
                        token,
                        devices,
                        dut,
                        peer,
                        dut_is_a,
                        spacing,
                        combos,
                        waves,
                        out,
                        records_path,
                    )
                if args.lbt:
                    run_block(
                        args,
                        token,
                        devices,
                        dut,
                        peer,
                        dut_is_a,
                        0.0,
                        [(0.0, args.rx_gains[0], args.bw)],
                        waves,
                        out,
                        records_path,
                    )
    finally:
        load_logger.stop()
        meta["max_load1"] = load_logger.max_load1
        meta["invalid_load"] = load_logger.invalid
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    print(f"records in {records_path}; next: si_scan.py summarize {out}")
    return 0


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_block(*arguments) -> None:
    """One block, retried up to 3 times when a Pluto fails (records of a retry win)."""

    spacing, dut = arguments[6], arguments[3]
    hw_common.with_retries(
        f"block S{spacing / 1e6:g}_{dut[-6:]}", lambda: _run_block_once(*arguments)
    )


def _run_block_once(
    args, token, devices, dut, peer, dut_is_a, spacing, combos, waves, out, records_path
) -> None:
    f_rx, f_tx = frequencies(spacing, dut_is_a)
    first_gain, first_bw = combos[0][1], combos[0][2]
    block_id = f"S{spacing / 1e6:g}_{dut[-6:]}"
    print(f"block {block_id}: RX {f_rx / 1e6:.3f} MHz TX {f_tx / 1e6:.3f} MHz", flush=True)
    load = hw_common.loadavg() if args.dry_run else hw_common.wait_for_load(args.max_load)

    def opened():
        block = Block(args, token, devices, dut, peer, f_rx, f_tx, first_gain, first_bw)
        block.open()
        return block

    block = opened()
    retries = 0
    try:
        lbt = spacing == 0.0
        peer_gains: dict[str, float] = {}
        calibration = {}
        if not lbt:
            for level, start in (("mid", -12.0), ("edge", -17.0)):
                calibration[level] = calibrate_peer(block, waves, SNR_TARGETS_DB[level], start)
                peer_gains[level] = calibration[level]["gain_db"]
        hw_common.append_jsonl(
            records_path,
            {
                "kind": "block",
                "block_id": block_id,
                "dut": dut,
                "peer": peer,
                "spacing_hz": spacing,
                "f_rx_hz": f_rx,
                "f_tx_hz": f_tx,
                "open_retries": retries,
                "startup_overflows_3s": block.startup_overflows,
                "calibration": calibration,
                "loadavg": load,
                "device": block.device_snapshot(),
            },
        )
        for spacing_hz, gain, bandwidth in combos:
            block.set_rx(gain, bandwidth)
            combo = {
                "combo_id": f"{block_id}_g{gain:g}_b{bandwidth / 1e6:g}",
                "block_id": block_id,
                "dut": dut,
                "peer": peer,
                "spacing_hz": spacing_hz,
                "f_rx_hz": f_rx,
                "f_tx_hz": f_tx,
                "rx_gain_db": gain,
                "rx_bw_hz": bandwidth,
            }
            phases = LBT_PHASES if lbt else PHASES
            for phase in phases:
                record = run_phase(block, phase, waves, peer_gains, combo, args, out)
                if phase.name in ("a", "b3", "lbt-own"):
                    record["device"] = block.device_snapshot()
                hw_common.append_jsonl(records_path, record)
                print(_one_line(record), flush=True)
            block.own_close()
    finally:
        block.close()


def _one_line(record: dict) -> str:
    noise = record.get("noise", {})
    peer = record.get("peer_rx", {})
    return (
        f"{record['combo_id']:<28} {record['phase']:<10} P_data {noise.get('p_data_dbfs')} "
        f"clip {record['saturation']['clip_fraction']:.2e} ovf {record['overflow_events']} "
        f"snr {peer.get('snr_median_db')} loss {peer.get('loss')} duty {record['duty_achieved']}"
    )


# --------------------------------------------------------------------------
# summarize: the decision rule, design §1.3
# --------------------------------------------------------------------------


def combo_metrics(phases: dict[str, dict]) -> dict[str, object]:
    """ΔN, ΔSNR, ΔLoss, clip and ΔOvf of one (S, DUT, g, b) at own TX -5 dB, duty 0.9."""

    def get(name, *path):
        value = phases.get(name)
        for key in path:
            if value is None:
                return None
            value = value.get(key)
        return value

    p_a, p_b3 = get("a", "noise", "p_data_dbfs"), get("b3", "noise", "p_data_dbfs")
    snr_c, snr_d = (
        get("c-mid", "peer_rx", "snr_median_db"),
        get("d-mid-m5", "peer_rx", "snr_median_db"),
    )
    loss_c, loss_d = get("c-edge", "peer_rx", "loss"), get("d-edge-m5", "peer_rx", "loss")
    ovf_a, ovf_b3 = get("a", "overflows_per_min"), get("b3", "overflows_per_min")
    slope = None
    gains, levels = [], []
    for index, gain in enumerate(OWN_GAINS_DB, start=1):
        level = get(f"b{index}", "noise", "p_data_dbfs")
        if level is not None:
            gains.append(gain)
            levels.append(level)
    if len(gains) >= 2:
        slope = round(float(np.polyfit(gains, levels, 1)[0]), 3)
    return {
        "dn_db": None if p_a is None or p_b3 is None else round(p_b3 - p_a, 2),
        "dn_idle_db": None
        if p_a is None or get("b0", "noise", "p_data_dbfs") is None
        else round(get("b0", "noise", "p_data_dbfs") - p_a, 2),
        "dn_0db_db": None
        if p_a is None or get("b4", "noise", "p_data_dbfs") is None
        else round(get("b4", "noise", "p_data_dbfs") - p_a, 2),
        "dn_duty03_db": None
        if p_a is None or get("b5", "noise", "p_data_dbfs") is None
        else round(get("b5", "noise", "p_data_dbfs") - p_a, 2),
        "noise_slope_db_per_db": slope,
        "dsnr_db": None if snr_c is None or snr_d is None else round(snr_c - snr_d, 2),
        "dsnr_0db_db": None
        if snr_c is None or get("d-mid-0", "peer_rx", "snr_median_db") is None
        else round(snr_c - get("d-mid-0", "peer_rx", "snr_median_db"), 2),
        "dloss_pp": None if loss_c is None or loss_d is None else round((loss_d - loss_c) * 100, 2),
        "clip": get("b3", "saturation", "clip_fraction"),
        "dovf_per_min": None if ovf_a is None or ovf_b3 is None else round(ovf_b3 - ovf_a, 3),
        "baseline_snr_db": snr_c,
        "baseline_loss_edge": loss_c,
        "p_data_a_dbfs": p_a,
        "occupancy_a": get("a", "noise", "occupancy_1ms"),
        "spur_guard_b3_db": get("b3", "noise", "spur_guard_db"),
        "spur_data_b3_db": get("b3", "noise", "spur_data_db"),
        "rssi_b3_db": get("b3", "rssi_db"),
    }


def verdict(metrics: dict, *, ignore_overflow: bool = False) -> str:
    def ok(key, limit):
        value = metrics.get(key)
        return value is not None and value <= limit

    def clip_ok(limit):
        value = metrics.get("clip")
        return value is not None and value < limit

    passes = (
        ok("dn_db", PASS["dn_db"])
        and ok("dsnr_db", PASS["dsnr_db"])
        and ok("dloss_pp", PASS["dloss_pp"])
        and clip_ok(PASS["clip"])
        and (ignore_overflow or ok("dovf_per_min", PASS["dovf_per_min"]))
    )
    if passes:
        return "PASS"
    if (
        ok("dsnr_db", MARGINAL["dsnr_db"])
        and ok("dloss_pp", MARGINAL["dloss_pp"])
        and clip_ok(MARGINAL["clip"])
    ):
        return "MARGINAL"
    return "FAIL"


_RANK = {"PASS": 0, "MARGINAL": 1, "FAIL": 2, "INCOMPLETE": 3}


def _threshold_14() -> float:
    try:
        from ofdm_message_link.dashboard import SNR_THRESHOLD_DB

        return float(SNR_THRESHOLD_DB[14])
    except Exception:  # noqa: BLE001 - the constant is the fallback
        return T14_DB


def decide(records: list[dict]) -> dict[str, object]:
    """Group the phase records, apply §1.3 to both DUTs, and pick the setting."""

    phases: dict[str, dict[str, dict]] = {}
    combos: dict[str, dict] = {}
    for record in records:
        if record.get("kind") != "phase":
            continue
        phases.setdefault(record["combo_id"], {})[record["phase"]] = record
        combos[record["combo_id"]] = record
    per_combo = {}
    for combo_id, by_phase in phases.items():
        info = combos[combo_id]
        if info["spacing_hz"] == 0.0:
            continue
        metrics = combo_metrics(by_phase)
        per_combo[combo_id] = {
            "dut": info["dut"],
            "spacing_hz": info["spacing_hz"],
            "rx_gain_db": info["rx_gain_db"],
            "rx_bw_hz": info["rx_bw_hz"],
            "metrics": metrics,
            "verdict": verdict(metrics),
            "verdict_without_overflow": verdict(metrics, ignore_overflow=True),
        }
    duts = sorted({entry["dut"] for entry in per_combo.values()})
    settings: dict[tuple, dict] = {}
    for entry in per_combo.values():
        key = (entry["spacing_hz"], entry["rx_gain_db"], entry["rx_bw_hz"])
        settings.setdefault(key, {})[entry["dut"]] = entry

    overflow_everywhere = bool(per_combo) and all(
        (entry["metrics"]["dovf_per_min"] or 0.0) > PASS["dovf_per_min"]
        for entry in per_combo.values()
    )
    field_name = "verdict_without_overflow" if overflow_everywhere else "verdict"

    table = []
    for key, by_dut in sorted(settings.items()):
        verdicts = [by_dut[dut][field_name] if dut in by_dut else "INCOMPLETE" for dut in duts]
        worst = max(verdicts, key=_RANK.__getitem__) if verdicts else "INCOMPLETE"
        snrs = [by_dut[d]["metrics"]["baseline_snr_db"] for d in by_dut]
        dsnrs = [by_dut[d]["metrics"]["dsnr_db"] for d in by_dut]
        table.append(
            {
                "spacing_hz": key[0],
                "rx_gain_db": key[1],
                "rx_bw_hz": key[2],
                "verdict": worst,
                "per_dut": {dut[-6:]: by_dut[dut][field_name] for dut in by_dut},
                "baseline_snr_db_min": min((s for s in snrs if s is not None), default=None),
                "dsnr_db_max": max((s for s in dsnrs if s is not None), default=None),
            }
        )

    def rows(spacing, verdict_name):
        return [
            row for row in table if row["spacing_hz"] == spacing and row["verdict"] == verdict_name
        ]

    t14 = _threshold_14()
    decision: dict[str, object]
    step1 = [
        row for row in rows(40e6, "PASS") if row["rx_gain_db"] == 15 and row["rx_bw_hz"] == 5e6
    ]
    if step1:
        decision = {
            "step": 1,
            "mode": "fdd",
            "frequencies_mhz": [2420, 2460],
            "rx_gain_db": 15,
            "rx_bw_hz": 5e6,
        }
    else:
        candidates = [
            row
            for row in rows(40e6, "PASS")
            if row["rx_gain_db"] >= 15 or (row["baseline_snr_db_min"] or 0) >= t14 + 3
        ]
        if candidates:
            best = max(candidates, key=lambda row: row["baseline_snr_db_min"] or -1e9)
            decision = {
                "step": 2,
                "mode": "fdd",
                "frequencies_mhz": [2420, 2460],
                "rx_gain_db": best["rx_gain_db"],
                "rx_bw_hz": best["rx_bw_hz"],
            }
        elif rows(70e6, "PASS"):
            best = max(rows(70e6, "PASS"), key=lambda row: row["baseline_snr_db_min"] or -1e9)
            decision = {
                "step": 3,
                "mode": "fdd",
                "frequencies_mhz": [2405, 2475],
                "rx_gain_db": best["rx_gain_db"],
                "rx_bw_hz": best["rx_bw_hz"],
            }
        elif rows(40e6, "MARGINAL") or rows(70e6, "MARGINAL"):
            best = min(
                rows(40e6, "MARGINAL") + rows(70e6, "MARGINAL"),
                key=lambda row: row["dsnr_db_max"] if row["dsnr_db_max"] is not None else 1e9,
            )
            pair = [2420, 2460] if best["spacing_hz"] == 40e6 else [2405, 2475]
            decision = {
                "step": 4,
                "mode": "fdd",
                "frequencies_mhz": pair,
                "rx_gain_db": best["rx_gain_db"],
                "rx_bw_hz": best["rx_bw_hz"],
                "penalty": f"MARGINAL: ΔSNR up to {best['dsnr_db_max']} dB "
                "(about one MCS rung with LA)",
            }
        else:
            decision = {
                "step": 5,
                "mode": "lbt",
                "frequency_mhz": quietest(records),
                "note": "antenna separation is a physical option before LBT; "
                "it needs the owner's approval",
            }
    decision["overflow_fails_everywhere"] = overflow_everywhere
    if overflow_everywhere:
        decision["overflow_note"] = (
            "ΔOvf failed at every RF setting: the cause is USB (M0), not RF; FDD stays"
        )
    return {
        "per_combo": per_combo,
        "table": table,
        "decision": decision,
        "lbt": lbt_summary(records),
        "occupancy": occupancy_by_frequency(records),
        "t14_db": t14,
    }


def occupancy_by_frequency(records: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for record in records:
        if record.get("kind") == "phase" and record["phase"] == "a":
            key = f"{record['f_rx_hz'] / 1e6:.0f}"
            entry = result.setdefault(key, {"occupancy_1ms": [], "p_data_dbfs": []})
            entry["occupancy_1ms"].append(record["noise"].get("occupancy_1ms"))
            entry["p_data_dbfs"].append(record["noise"].get("p_data_dbfs"))
    return result


def quietest(records: list[dict]) -> int:
    occupancy = occupancy_by_frequency(records)
    best, best_key = None, 2440
    for frequency in ("2420", "2440", "2460"):
        values = [
            value
            for value in occupancy.get(frequency, {}).get("occupancy_1ms", [])
            if value is not None
        ]
        if values and (best is None or float(np.mean(values)) < best):
            best, best_key = float(np.mean(values)), int(frequency)
    return best_key


def lbt_summary(records: list[dict]) -> list[dict]:
    return [
        {
            "dut": record["dut"][-6:],
            **record.get("recovery", {}),
            "clip_fraction": record["saturation"]["clip_fraction"],
            "max_abs_lsb": record["saturation"]["max_abs_lsb"],
        }
        for record in records
        if record.get("kind") == "phase" and record["phase"] == "lbt-own"
    ]


def summary_markdown(result: dict, meta: dict) -> str:
    lines = ["# M1 self-interference scan: decision table", ""]
    decision = result["decision"]
    lines.append(f"Decision (design §1.3, step {decision['step']}): `{json.dumps(decision)}`")
    lines.append("")
    lines.append(
        "Criteria at own TX -5 dB, duty 0.9, both DUTs: PASS ΔN ≤ 1.0 dB, ΔSNR ≤ 1.0 dB, "
        "ΔLoss ≤ 0.5 pp, clip < 1e-6, ΔOvf ≤ 0.5/min; MARGINAL ΔSNR ≤ 3.0 dB, ΔLoss ≤ 2 pp, "
        "clip < 1e-4."
    )
    lines.append("")
    lines.append("| S MHz | g dB | b MHz | verdict | per DUT | baseline SNR min | ΔSNR max |")
    lines.append("|---:|---:|---:|---|---|---:|---:|")
    for row in result["table"]:
        lines.append(
            f"| {row['spacing_hz'] / 1e6:g} | {row['rx_gain_db']:g} | {row['rx_bw_hz'] / 1e6:g} | "
            f"{row['verdict']} | {row['per_dut']} | {row['baseline_snr_db_min']} | "
            f"{row['dsnr_db_max']} |"
        )
    lines += ["", "## Per combination", ""]
    lines.append(
        "| combo | ΔN | ΔN idle | ΔN 0 dB | ΔN D0.3 | slope | ΔSNR | ΔSNR 0 dB | ΔLoss pp | clip "
        "| ΔOvf/min | base SNR | occ. a | spur guard | verdict |"
    )
    lines.append("|---|" + "---:|" * 13 + "---|")
    for combo_id, entry in sorted(result["per_combo"].items()):
        m = entry["metrics"]
        lines.append(
            f"| {combo_id} | {m['dn_db']} | {m['dn_idle_db']} | {m['dn_0db_db']} | "
            f"{m['dn_duty03_db']} | {m['noise_slope_db_per_db']} | {m['dsnr_db']} | "
            f"{m['dsnr_0db_db']} | {m['dloss_pp']} | {m['clip']} | {m['dovf_per_min']} | "
            f"{m['baseline_snr_db']} | {m['occupancy_a']} | {m['spur_guard_b3_db']} | "
            f"{entry['verdict']} |"
        )
    lines += ["", "## Phase-a occupancy per frequency (1-ms blocks > floor + 6 dB)", ""]
    for frequency, entry in sorted(result["occupancy"].items()):
        lines.append(
            f"* {frequency} MHz: {entry['occupancy_1ms']}, P_data {entry['p_data_dbfs']} dBFS"
        )
    if result["lbt"]:
        lines += ["", "## S = 0 (both 2440 MHz, own TX -20 dB)", ""]
        for entry in result["lbt"]:
            lines.append(f"* {json.dumps(entry)}")
    lines += [
        "",
        f"Run: git {meta.get('git_revision', '')[:10]}, load at start "
        f"{meta.get('loadavg_at_start')}, max load1 {meta.get('max_load1')}, invalid load "
        f"{meta.get('invalid_load')}.",
        "",
        "```",
        str(meta.get("lsusb_t", "")).rstrip(),
        "```",
    ]
    return "\n".join(lines) + "\n"


def summarize(args) -> int:
    directory: Path = args.directory
    records = hw_common.read_jsonl(directory / "records.jsonl")
    meta_path = directory / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    result = decide(records)
    output = {"meta": meta, "records": records, **result}
    (directory / "si_scan.json").write_text(json.dumps(output, indent=1, default=str))
    (directory / "si_summary.md").write_text(summary_markdown(result, meta))
    print(json.dumps(result["decision"], indent=1))
    return 0


def _mhz_list(text: str) -> list[float]:
    return [float(value) * 1e6 for value in text.split(",") if value]


def _float_list(text: str) -> list[float]:
    return [float(value) for value in text.split(",") if value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="measure (needs both Plutos and RF consent)")
    run_parser.add_argument("--dut", default=hw_common.NODE_A_SERIAL)
    run_parser.add_argument("--peer", default=hw_common.NODE_B_SERIAL)
    run_parser.add_argument(
        "--node-a",
        default=hw_common.NODE_A_SERIAL,
        help="the serial that transmits on the lower frequency",
    )
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument(
        "--spacings",
        type=_mhz_list,
        default=[20e6, 40e6, 70e6],
        help="MHz, comma separated (default 20,40,70)",
    )
    run_parser.add_argument("--rx-gains", type=_float_list, default=[15.0, 30.0, 45.0])
    run_parser.add_argument("--bw", type=float, default=5e6, help="RX analog bandwidth, Hz")
    run_parser.add_argument(
        "--extra-bw",
        type=_mhz_list,
        default=[4e6, 8e6],
        help="extra RX bandwidths at S=40, first gain (MHz; '' for none)",
    )
    run_parser.add_argument("--seconds", type=float, default=2.0)
    run_parser.add_argument("--rx-kernel-buffers", type=int, default=4)
    run_parser.add_argument(
        "--single-dut", action="store_true", help="do not repeat with the roles swapped"
    )
    run_parser.add_argument(
        "--no-lbt", dest="lbt", action="store_false", help="skip the S = 0 own-burst measurement"
    )
    run_parser.add_argument("--keep-raw", action="store_true")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="no radios: synthetic captures, for testing the pipeline",
    )
    hw_common.add_rf_arguments(run_parser)
    summary_parser = commands.add_parser("summarize", help="decision table from a run directory")
    summary_parser.add_argument("directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run(args)
    return summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())

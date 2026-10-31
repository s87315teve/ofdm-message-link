"""Decode recorded captures (v5 or legacy) with the current receiver, with a per-stage loss split.

Feeds each ``.npy`` (int16 interleaved, written by ``ota_capture_v5.py`` or
``experiments/rx_improvement/ota_capture.py``) through the app's own
``MessageReceiver`` in Pluto-sized chunks (65536 samples), exactly as rx_app
does, and reports per recording: sent, decoded, loss, and where the lost
bursts died:

* ``preamble_missed``  no acquisition near the burst's position,
* ``sig_failed``       acquired, but the SIG / legacy header never validated,
* ``payload_failed``   header valid, payload failed (v5: FCS failed and no
  CRC-valid subframe).

Attribution: every decode attempt the streaming decoder makes is tapped
(position, family, outcome).  Decoded bursts give (sequence, position); each
missing sequence's position is interpolated from its decoded neighbours and
the attempts within +-``--window`` samples of it decide its stage.  With fewer
than 5 decoded bursts positions cannot be predicted and attempts are grouped
into clusters instead (``attribution: clusters``).

SNR: every preamble SNR reading (v5: LTF pair, legacy: chirp halves), as
improve-rx reported it (median, IQR, count), plus the readings at attributed
burst positions only.  ``--set key=value`` overrides config keys (dotted
paths, YAML values), e.g. ``--set phy.rx.ltf_average=false``.

    PYTHONPATH=src python experiments/tx_v5/offline_decode_v5.py <dir-or-npy>... --out res.json \\
        [--set phy.rx.v5_acquisition=false] [--only 'v5m8_*.npy'] [--workers 1]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import yaml

import ofdm_link.phy.streaming as streaming
from ofdm_link.config import _deep_merge, _read_yaml, validate_config
from ofdm_link.phy import MCS
from ofdm_link.phy.ampdu import unpack_ampdu
from ofdm_link.phy.burst import BurstDecodeError, DecodedBurst
from ofdm_link.phy.codec import FrameDecodeError, deserialize_frame
from ofdm_link.phy.sync_v5 import V5Acquisition
from ofdm_message_link.link import MessageReceiver, build_phy_profile
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

CHUNK = 65536  # pluto._RX_CHUNK_SAMPLES
RX_FULL_SCALE = 2048.0


class Tap:
    """Records every decode attempt of the one active streaming decoder."""

    decoder = None
    attempts: list[dict] = []
    snrs: list[tuple[int, str, float]] = []


_ORIGINAL_DECODE = streaming._decode_burst_with_acquisition


def _sequences(burst) -> list[int]:
    if isinstance(burst, DecodedBurst):
        return [int(burst.frame.sequence)]
    seqs = []
    for mpdu in unpack_ampdu(burst.body).mpdus:
        try:
            seqs.append(int(deserialize_frame(mpdu).sequence))
        except FrameDecodeError:
            continue
    return seqs


def _tapped_decode(samples, config, acquisition, **kwargs):
    decoder = Tap.decoder
    base = 0 if decoder is None else decoder._buffer_start
    start = base + int(acquisition.preamble_start)
    family = "v5" if isinstance(acquisition, V5Acquisition) else "legacy"
    record = {"start": start, "family": family}
    try:
        burst = _ORIGINAL_DECODE(samples, config, acquisition, **kwargs)
    except BurstDecodeError as error:
        message = str(error)
        if error.required_sample_count is not None and error.required_sample_count > len(samples):
            record["kind"] = "need_more"
        elif error.diagnostics is not None:
            record["kind"] = "payload_fail"
        elif message.startswith("fixed signaling header decode failed:") or message.startswith(
            "signaled "
        ):
            record["kind"] = "header_fail"
        else:
            record["kind"] = "other_fail"
        record["msg"] = message[:80]
        Tap.attempts.append(record)
        raise
    fcs_ok = bool(getattr(burst, "fcs_ok", True))
    seqs = _sequences(burst)
    values = np.asarray(samples)
    span = values[burst.burst_start : burst.burst_end]
    record.update(
        kind="ok" if fcs_ok else "fcs_fail",
        seqs=seqs,
        end=base + int(burst.burst_end),
        power=float(np.mean(np.abs(span) ** 2)) if span.size else None,
    )
    Tap.attempts.append(record)
    return burst


streaming._decode_burst_with_acquisition = _tapped_decode


def build_config(overlays: list[str], sets: list[str]):
    raw = _read_yaml(DEFAULT_CONFIG)
    for overlay in overlays:
        raw = _deep_merge(raw, _read_yaml(overlay))
    for item in sets:
        key, _, value = item.partition("=")
        node = raw
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(value)
    return validate_config(raw)


def _stats(values) -> dict:
    if not values:
        return {"median": None, "iqr": None, "count": 0}
    arr = np.asarray(values, dtype=float)
    q75, q25 = np.percentile(arr, [75, 25])
    return {"median": float(np.median(arr)), "iqr": float(q75 - q25), "count": int(arr.size)}


def _db(x):
    return None if x is None or x <= 0 else float(10 * np.log10(x))


def attribute(sent: int, decoded: set[int], attempts: list[dict], snrs, window: int) -> dict:
    """Split the lost bursts into preamble / SIG / payload stages."""

    pos: dict[int, int] = {}
    for record in attempts:
        if record["kind"] in ("ok", "fcs_fail"):
            for seq in record.get("seqs", []):
                if 0 <= seq < sent and seq not in pos:
                    pos[seq] = record["start"]
    lost = [s for s in range(sent) if s not in decoded]
    out: dict = {"lost": len(lost)}
    if len(pos) >= 5:
        seqs = np.array(sorted(pos))
        starts = np.array([pos[s] for s in seqs], dtype=float)
        # Robust line for extrapolation beyond the decoded range.
        coef = np.polyfit(seqs, starts, 1)
        for _ in range(2):
            resid = starts - np.polyval(coef, seqs)
            keep = np.abs(resid) < max(5 * np.median(np.abs(resid)), 500)
            if keep.sum() >= 5:
                coef = np.polyfit(seqs[keep], starts[keep], 1)

        def predict(seq: int) -> float:
            if seqs[0] <= seq <= seqs[-1]:
                return float(np.interp(seq, seqs, starts))
            return float(np.polyval(coef, seq))

        # Leave-one-out jitter of the interpolation on decoded bursts.
        loo = []
        for i in range(1, len(seqs) - 1):
            p = np.interp(seqs[i], [seqs[i - 1], seqs[i + 1]], [starts[i - 1], starts[i + 1]])
            loo.append(starts[i] - p)
        loo = np.abs(np.asarray(loo)) if loo else np.zeros(1)
        out.update(
            attribution="positions",
            spacing_samples=float(coef[0]),
            loo_jitter_samples_p50=float(np.percentile(loo, 50)),
            loo_jitter_samples_p99=float(np.percentile(loo, 99)),
            loo_jitter_samples_max=float(loo.max()),
            window_samples=window,
        )
        a_starts = np.array([r["start"] for r in attempts], dtype=float)
        stage = {"preamble_missed": 0, "sig_failed": 0, "payload_failed": 0, "decoded_elsewhere": 0}
        lost_detail = []
        burst_snrs = []
        snr_pos = np.array([s[0] for s in snrs], dtype=float)
        snr_val = np.array([s[2] for s in snrs], dtype=float)
        for seq in range(sent):
            p = predict(seq)
            if snr_pos.size:
                j = int(np.argmin(np.abs(snr_pos - p)))
                if abs(snr_pos[j] - p) <= window:
                    burst_snrs.append(float(snr_val[j]))
            if seq in decoded:
                continue
            near = (
                [attempts[i] for i in np.flatnonzero(np.abs(a_starts - p) <= window)]
                if a_starts.size
                else []
            )
            kinds = {r["kind"] for r in near}
            if kinds & {"ok"}:
                which = "decoded_elsewhere"  # another burst decoded here (should not happen)
            elif kinds & {"need_more", "payload_fail", "fcs_fail"}:
                which = "payload_failed"
            elif kinds & {"header_fail", "other_fail"}:
                which = "sig_failed"
            else:
                which = "preamble_missed"
            stage[which] += 1
            lost_detail.append([seq, which])
        out.update(stage)
        out["lost_detail"] = lost_detail
        out["burst_snr"] = _stats(burst_snrs)
    else:
        # Cluster the attempts; each cluster is one physical preamble.
        clusters: list[list[dict]] = []
        for record in sorted(attempts, key=lambda r: r["start"]):
            if clusters and record["start"] - clusters[-1][-1]["start"] <= window:
                clusters[-1].append(record)
            else:
                clusters.append([record])
        dec = sig = pay = 0
        for cluster in clusters:
            kinds = {r["kind"] for r in cluster}
            if "ok" in kinds:
                dec += 1
            elif kinds & {"need_more", "payload_fail", "fcs_fail"}:
                pay += 1
            elif kinds & {"header_fail", "other_fail"}:
                sig += 1
        out.update(
            attribution="clusters",
            payload_failed=pay,
            sig_failed=sig,
            preamble_missed=max(0, len(lost) - pay - sig),
            clusters=len(clusters),
            burst_snr=_stats([]),
        )
    return out


def noise_floor(samples: np.ndarray, block: int = 4096) -> float:
    n = samples.size // block * block
    p = np.mean((np.abs(samples[:n]) ** 2).reshape(-1, block), axis=1)
    return float(np.percentile(p, 10))


def decode(path: Path, overlays: list[str], sets: list[str], workers: int, window: int) -> dict:
    meta = json.loads(path.with_suffix(".json").read_text())
    config = build_config(overlays, sets)
    modulation = MCS[meta["mcs"].upper()]
    profile = build_phy_profile(config, mcs=modulation)
    receiver = MessageReceiver(profile, max_observations=1 << 16, decode_workers=workers)
    Tap.decoder = receiver._decoder
    Tap.attempts = []
    Tap.snrs = []
    decoder = receiver._decoder
    original_note = decoder._note_acquisition

    def note(acquisition):
        before = len(decoder._link_snrs)
        original_note(acquisition)
        if len(decoder._link_snrs) > before:
            family = "v5" if isinstance(acquisition, V5Acquisition) else "legacy"
            Tap.snrs.append(
                (decoder._buffer_start + int(acquisition.preamble_start), family,
                 float(decoder._link_snrs[-1]))
            )

    decoder._note_acquisition = note
    raw = np.load(path).astype(np.float32) / RX_FULL_SCALE
    samples = raw.view(np.complex64)
    sequences: set[int] = set()
    evm_snr: list[float] = []
    preamble_snr: list[float] = []
    started = time.monotonic()

    def take(observations):
        for observation in observations:
            if observation.is_ours:
                sequences.add(observation.sequence)
            if observation.effective_snr_db is not None:
                evm_snr.append(observation.effective_snr_db)

    for start in range(0, samples.size, CHUNK):
        receiver.feed(samples[start : start + CHUNK])
        take(receiver.drain_observations())
        preamble_snr.extend(receiver.drain_link_snr_db())
    receiver.flush(timeout=60)
    take(receiver.drain_observations())
    preamble_snr.extend(receiver.drain_link_snr_db())
    receiver.close()
    elapsed = time.monotonic() - started
    snapshot = receiver.decoder_snapshot()
    sent = int(meta["bursts_sent"])
    in_range = {s for s in sequences if s < sent}

    # Powers: decoded-burst spans vs the idle floor.
    floor = noise_floor(samples)
    powers = [r["power"] for r in Tap.attempts if r["kind"] == "ok" and r.get("power")]
    p_burst = float(np.median(powers)) if powers else None
    stages = attribute(sent, in_range, Tap.attempts, Tap.snrs, window)
    pre = _stats(preamble_snr)
    stem = path.stem
    result = {
        "file": path.name,
        "stem": stem,
        "family": meta.get("family", "legacy"),
        "mcs": meta["mcs"],
        "mcs_index": meta.get("mcs_index"),
        "tx_gain_db": meta["tx_gain_db"],
        "guards": meta.get("guards"),
        "scrambler": meta.get("scrambler"),
        "payload": meta.get("payload", "random"),
        "sets": sets,
        "sent": sent,
        "decoded": len(in_range),
        "loss": 1.0 - len(in_range) / sent,
        "loss_ci95": wilson(sent - len(in_range), sent),
        "preamble_missed": stages.get("preamble_missed"),
        "sig_failed": stages.get("sig_failed"),
        "payload_failed": stages.get("payload_failed"),
        "stage_attribution": stages.get("attribution"),
        "rx_overflow_events": meta.get("rx_overflow_events"),
        "loadavg_at_capture": meta.get("loadavg_at_start"),
        "candidates": snapshot.detected_burst_candidates,
        "candidates_v5": snapshot.detected_v5_candidates,
        "candidates_legacy": snapshot.detected_legacy_candidates,
        "header_ok": snapshot.header_decode_success,
        "header_fail": snapshot.header_decode_failure,
        "payload_fail": snapshot.payload_decode_failure,
        "psdu_fcs_failures": snapshot.psdu_fcs_failures,
        "bursts_v5": snapshot.bursts_v5,
        "bursts_legacy": snapshot.bursts_legacy,
        "evm_snr_db_median": float(np.median(evm_snr)) if evm_snr else None,
        "preamble_snr_db_median": pre["median"],
        "preamble_snr_db_iqr": pre["iqr"],
        "preamble_snr_count": pre["count"],
        "burst_snr_db_median": stages["burst_snr"]["median"],
        "burst_snr_db_iqr": stages["burst_snr"]["iqr"],
        "burst_snr_count": stages["burst_snr"]["count"],
        "noise_floor_dbfs": _db(floor),
        "decoded_burst_power_dbfs_median": _db(p_burst),
        "decoded_burst_wideband_snr_db": _db((p_burst - floor) / floor) if p_burst else None,
        "decode_seconds": elapsed,
        "workers": workers,
        "stage_detail": {k: v for k, v in stages.items() if k not in ("burst_snr",)},
    }
    return result


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [float(max(0.0, centre - half)), float(min(1.0, centre + half))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", required=True)
    parser.add_argument("--overlay", action="append")
    parser.add_argument("--set", dest="sets", action="append", default=[])
    parser.add_argument("--only", default="*.npy")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--window", type=int, default=10000,
                        help="attribution window around a predicted burst start (samples)")
    parser.add_argument("--append", action="store_true", help="add to an existing output file")
    args = parser.parse_args()
    overlays = args.overlay or [DEFAULT_OVERLAY]
    paths: list[Path] = []
    for item in args.inputs:
        paths.extend(sorted(item.glob(args.only)) if item.is_dir() else [item])
    results = []
    if args.append and Path(args.out).exists():
        results = json.loads(Path(args.out).read_text())
    for path in paths:
        if not path.with_suffix(".json").exists():
            print(f"skip {path}: no .json", flush=True)
            continue
        result = decode(path, overlays, args.sets, args.workers, args.window)
        results.append(result)
        brief = {k: result[k] for k in (
            "file", "sets", "sent", "decoded", "loss", "preamble_missed", "sig_failed",
            "payload_failed", "preamble_snr_db_median", "burst_snr_db_median",
            "evm_snr_db_median", "candidates", "decode_seconds")}
        print(json.dumps(brief), flush=True)
        Path(args.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()

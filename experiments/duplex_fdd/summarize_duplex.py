"""Summarize bidirectional FDD runs (run_duplex.sh / video_duplex.sh directories).

    python summarize_duplex.py RUN_DIR [RUN_DIR ...] [--no-plots]

Writes RUN_DIR/summary.json, summary.md and duplex_{AB,BA}.png.  Direction AB is
node 1 -> node 2 (sender node 1, receiver node 2); BA is the reverse.

Definitions
  * time axis: seconds from node ready (``elapsed_s`` of the stats lines); LA log
    times are monotonic and mapped through the sender's stats (elapsed - monotonic).
  * segments: bounded by the gain-profile step times of both nodes (30-s blocks when
    neither node has a profile).
  * goodput: receiver ``message_bytes_delivered`` deltas.
  * subframe loss: receiver ``missing_bursts`` / (``bursts_decoded`` + ``missing_bursts``)
    deltas (frames are counted per sequence gap; ``subframes_bad`` is reported apart).
  * datagram loss per segment: 1 - receiver egress datagrams / sender ingress datagrams
    (delivery latency makes small negative values possible, clipped at 0); the run
    total comes from udp_load's byte-exact check (load_{AB,BA}.json).
  * convergence after a gain step: the first 1-s window after which every window up to
    the next step (or the end) has loss < 2 % and no MCS change within 2 s of its start.
  * RX overflow: ``rx.transport.rx_overflow_events`` after the first ``startup_s`` s.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))

DIRECTIONS = {"AB": ("1", "2"), "BA": ("2", "1")}  # direction -> (sender node, receiver node)
LOSS_LIMIT = 0.02
MCS_STABLE_S = 2.0
SEGMENT_S = 30.0
STARTUP_S = 3.0


# ---- reading -------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    """Records of a JSONL file; missing file -> []; torn or foreign lines are skipped."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def get(rec: dict, dotted: str, default=None):
    cur = rec
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def parse_profile(text: str | None) -> list[tuple[float, float]]:
    """'0:-5,30:-20' -> [(0.0, -5.0), (30.0, -20.0)], sorted by time."""
    if not text:
        return []
    steps = []
    for item in text.split(","):
        item = item.strip()
        if item:
            t, _, db = item.partition(":")
            steps.append((float(t), float(db)))
    return sorted(steps)


def gain_steps(profile: list[tuple[float, float]]) -> list[dict]:
    """Steps that change the gain (t > 0 or a different value than the previous one)."""
    out = []
    for (_, prev), (t, db) in zip(profile, profile[1:], strict=False):
        if db != prev:
            out.append({"t": t, "from_db": prev, "to_db": db})
    return out


def segment_edges(profiles: list[list[tuple[float, float]]], duration: float) -> list[float]:
    times = sorted({t for prof in profiles for t, _ in prof if 0 < t < duration})
    if not times:
        times = [
            k * SEGMENT_S
            for k in range(1, int(duration // SEGMENT_S) + 1)
            if k * SEGMENT_S < duration
        ]
    return [0.0, *times, duration]


# ---- time series -----------------------------------------------------------------------


class Series:
    """Cumulative counters sampled at increasing times; ``at`` is a step function."""

    def __init__(self, times: list[float], values: dict[str, list[float]]) -> None:
        self.t = times
        self.v = values

    def __bool__(self) -> bool:
        return bool(self.t)

    def at(self, key: str, t: float) -> float:
        col = self.v[key]
        lo, hi = 0, len(self.t)
        while lo < hi:  # last index with self.t[i] <= t
            mid = (lo + hi) // 2
            if self.t[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return col[max(lo - 1, 0)]

    def delta(self, key: str, t0: float, t1: float) -> float:
        return self.at(key, t1) - self.at(key, t0)


RX_KEYS = {
    "bytes": "rx.receiver.message_bytes_delivered",
    "decoded": "rx.receiver.bursts_decoded",
    "missing": "rx.receiver.missing_bursts",
    "sub_bad": "rx.receiver.subframes_bad",
    "egress": "rx.egress_datagrams",
    "overflow": "rx.transport.rx_overflow_events",
    "ingress": "tx.ingress_datagrams",
    "tx_mcs": "tx.active_mcs",
}


def build_series(stats: list[dict], offset: float | None = None) -> Series:
    """``offset`` (elapsed - monotonic of another node) puts the times on that node's axis.

    Each node counts ``elapsed_s`` from its own ready, and the two nodes become ready at
    different moments; the monotonic clock is shared.  A direction's receiver series must
    be on the sender's axis, where the sender's gain steps are.
    """

    recs = [r for r in stats if isinstance(get(r, "elapsed_s"), int | float)]
    if offset is not None:
        recs = [r for r in recs if isinstance(r.get("monotonic_s"), int | float)]
        recs = [{**r, "elapsed_s": float(r["monotonic_s"]) + offset} for r in recs]
    recs.sort(key=lambda r: r["elapsed_s"])
    times = [float(r["elapsed_s"]) for r in recs]
    values: dict[str, list[float]] = {}
    for name, path in RX_KEYS.items():
        last = 0.0
        col = []
        for r in recs:
            val = get(r, path)
            if isinstance(val, int | float):
                last = float(val)
            col.append(last)
        values[name] = col
    return Series(times, values)


def clock_offset(stats: list[dict]) -> float:
    """elapsed_s - monotonic_s (median), to place monotonic LA-log times on the elapsed axis."""
    diffs = [
        r["elapsed_s"] - r["monotonic_s"]
        for r in stats
        if isinstance(r.get("elapsed_s"), int | float)
        and isinstance(r.get("monotonic_s"), int | float)
    ]
    return median(diffs) if diffs else 0.0


def la_events(la: list[dict], offset: float) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"la": [], "feedback": [], "mark": []}
    for rec in la:
        kind = rec.get("event")
        if kind in out and isinstance(rec.get("t"), int | float):
            out[kind].append({**rec, "t_el": rec["t"] + offset})
    for lst in out.values():
        lst.sort(key=lambda r: r["t_el"])
    return out


def mcs_trajectory(events: list[dict], first_mcs: int | None) -> list[tuple[float, int]]:
    """[(t, mcs)] with the initial MCS at t = -inf represented by the first event's 'previous'."""
    traj: list[tuple[float, int]] = []
    if events:
        prev = events[0].get("previous")
        if isinstance(prev, int):
            traj.append((float("-inf"), prev))
        elif first_mcs is not None:
            traj.append((float("-inf"), first_mcs))
    elif first_mcs is not None:
        traj.append((float("-inf"), first_mcs))
    for ev in events:
        if isinstance(ev.get("mcs_index"), int):
            traj.append((ev["t_el"], ev["mcs_index"]))
    return traj


def mcs_at(traj: list[tuple[float, int]], t: float) -> int | None:
    cur = None
    for tt, m in traj:
        if tt <= t:
            cur = m
        else:
            break
    return cur


# ---- analysis ----------------------------------------------------------------------------


def loss_in(series: Series, t0: float, t1: float) -> float | None:
    miss = series.delta("missing", t0, t1)
    done = series.delta("decoded", t0, t1)
    return miss / (miss + done) if miss + done > 0 else None


def segment_stats(
    rx: Series, tx: Series, traj, feedback, la_changes, lo: float, hi: float, tx_gain: float | None
) -> dict:
    span = max(hi - lo, 1e-9)
    ingress = tx.delta("ingress", lo, hi)
    egress = rx.delta("egress", lo, hi)
    mcs_vals = [mcs_at(traj, lo)] + [m for t, m in traj if lo < t < hi]
    mcs_vals = [m for m in mcs_vals if m is not None]
    snrs = [
        f["snr_mean_db"]
        for f in feedback
        if lo <= f["t_el"] < hi and f.get("snr_mean_db") is not None
    ]
    snr_f = [
        f["snr_f_db"] for f in feedback if lo <= f["t_el"] < hi and f.get("snr_f_db") is not None
    ]
    offs = [
        f["offset_db"] for f in feedback if lo <= f["t_el"] < hi and f.get("offset_db") is not None
    ]
    return {
        "t0": lo,
        "t1": hi,
        "tx_gain_db": tx_gain,
        "goodput_mbps": rx.delta("bytes", lo, hi) * 8 / span / 1e6,
        "datagram_loss": max(0.0, 1 - egress / ingress) if ingress > 0 else None,
        "subframe_loss": loss_in(rx, lo, hi),
        "subframes_bad": rx.delta("sub_bad", lo, hi),
        "mcs_min": min(mcs_vals) if mcs_vals else None,
        "mcs_max": max(mcs_vals) if mcs_vals else None,
        "mcs_median": median(mcs_vals) if mcs_vals else None,
        "mcs_final": mcs_at(traj, hi - 1e-9),
        "mcs_changes": sum(1 for t in la_changes if lo <= t < hi),
        "snr_mean_db": sum(snrs) / len(snrs) if snrs else None,
        "snr_f_db": sum(snr_f) / len(snr_f) if snr_f else None,
        "offset_db": median(offs) if offs else None,
    }


def convergence(
    rx: Series, la_changes: list[float], step_t: float, bound: float, loaded: bool
) -> float | None:
    """Seconds after ``step_t`` until every later 1-s window up to ``bound`` is good."""
    if not loaded:
        return None
    windows = []
    t = step_t
    while t + 1.0 <= bound + 1e-9:
        loss = loss_in(rx, t, t + 1.0)
        loss_bad = loss is None or loss >= LOSS_LIMIT  # no frames at all under load = bad
        mcs_bad = any(t <= c < min(t + MCS_STABLE_S, bound) for c in la_changes)
        windows.append((t, loss_bad or mcs_bad))
        t += 1.0
    if not windows or windows[-1][1]:
        return None
    last_bad = max((i for i, (_, bad) in enumerate(windows) if bad), default=-1)
    return windows[last_bad + 1][0] - step_t


def analyse_direction(
    name: str,
    meta: dict,
    stats: dict[str, list[dict]],
    la: dict[str, list[dict]],
    load: dict | None,
    duration: float,
    edges: list[float],
) -> dict | None:
    src, dst = DIRECTIONS[name]
    sender_offset = clock_offset(stats.get(src, []))
    rx = build_series(stats.get(dst, []), offset=sender_offset)
    tx = build_series(stats.get(src, []))
    if not rx or not tx:
        return None
    events = la_events(la.get(src, []), sender_offset)
    first_mcs = int(tx.v["tx_mcs"][0]) if tx.v["tx_mcs"] else None
    traj = mcs_trajectory(events["la"], first_mcs)
    la_changes = [e["t_el"] for e in events["la"]]
    profile = parse_profile((meta.get("gain_profile") or {}).get(src))
    configured_rate = get(meta, f"load.{name}.rate")
    loaded = bool(load) or bool(configured_rate)

    def gain_at(t: float) -> float | None:
        cur = (meta.get("tx_gain_db") or {}).get(src)
        for tt, db in profile:
            if tt <= t:
                cur = db
        return cur

    segments = [
        segment_stats(rx, tx, traj, events["feedback"], la_changes, lo, hi, gain_at(lo + 1e-6))
        for lo, hi in zip(edges, edges[1:], strict=False)
    ]
    steps = []
    all_steps = gain_steps(profile)
    for i, step in enumerate(all_steps):
        bound = all_steps[i + 1]["t"] if i + 1 < len(all_steps) else duration
        steps.append(
            {
                **step,
                "convergence_s": convergence(rx, la_changes, step["t"], bound, loaded),
                "missing_first_1s": rx.delta("missing", step["t"], step["t"] + 1.0),
            }
        )
    bins = []
    for k in range(int(duration)):
        bins.append(
            {
                "t": float(k),
                "goodput_mbps": rx.delta("bytes", k, k + 1) * 8 / 1e6,
                "loss": loss_in(rx, k, k + 1),
            }
        )
    result = {
        "sender": f"node{src}",
        "receiver": f"node{dst}",
        "load": {
            k: load.get(k)
            for k in (
                "rate_offered",
                "offered_mbps",
                "sent",
                "delivered",
                "lost",
                "loss_ratio",
                "corrupt",
                "goodput_mbps",
                "steady_goodput_mbps",
                "latency_ms_p50",
                "latency_ms_p99",
            )
        }
        if load
        else None,
        "overall": segment_stats(rx, tx, traj, events["feedback"], la_changes, 0.0, duration, None),
        "segments": segments,
        "steps": steps,
        "marks": [{"t": m["t_el"], "text": m.get("text")} for m in events["mark"]],
        "bins": bins,
        "mcs_trajectory": [{"t": t, "mcs": m} for t, m in traj if t != float("-inf")],
        "feedback": [
            {
                "t": f["t_el"],
                "snr_mean_db": f.get("snr_mean_db"),
                "snr_f_db": f.get("snr_f_db"),
                "offset_db": f.get("offset_db"),
            }
            for f in events["feedback"]
        ],
        "initial_mcs": traj[0][1] if traj and traj[0][0] == float("-inf") else first_mcs,
    }
    return result


def overflow_report(stats: list[dict], startup_s: float) -> dict | None:
    ser = build_series(stats)
    if not ser:
        return None
    end = ser.t[-1]
    return {
        "startup": ser.at("overflow", startup_s),
        "loaded": ser.at("overflow", end) - ser.at("overflow", startup_s),
        "total": ser.at("overflow", end),
    }


def cpu_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    import cpu_multi

    try:
        return cpu_multi.summarize(str(path))
    except (OSError, ValueError, KeyError):
        return None


def summarize_run(run: Path) -> dict:
    meta_path = run / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    stats = {n: read_jsonl(run / f"node{n}_stats.jsonl") for n in ("1", "2")}
    la = {n: read_jsonl(run / f"la_{n}.jsonl") for n in ("1", "2")}
    loads = {}
    for d in DIRECTIONS:
        path = run / f"load_{d}.json"
        loads[d] = json.loads(path.read_text()) if path.exists() else None
    duration = max(
        (
            s["elapsed_s"]
            for recs in stats.values()
            for s in recs
            if isinstance(s.get("elapsed_s"), int | float)
        ),
        default=0.0,
    )
    profiles = [parse_profile((meta.get("gain_profile") or {}).get(n)) for n in ("1", "2")]
    edges = segment_edges(profiles, duration)
    startup_s = float(meta.get("startup_s", STARTUP_S))
    summary = {
        "run": str(run),
        "duration_s": duration,
        "meta": {
            k: meta.get(k)
            for k in (
                "git_rev",
                "serials",
                "overlay",
                "gain_profile",
                "tx_gain_db",
                "rx_gain_db",
                "mcs_index",
                "link_adaptation",
                "link_adaptation_args",
                "decode_workers",
                "loadavg_at_start",
                "load",
                "args",
            )
        },
        "directions": {},
        "rx_overflow": {n: overflow_report(stats[n], startup_s) for n in ("1", "2")},
        "cpu": cpu_report(run / "cpu.jsonl"),
    }
    for d in DIRECTIONS:
        res = analyse_direction(d, meta, stats, la, loads[d], duration, edges)
        if res is not None:
            summary["directions"][d] = res
    return summary


# ---- output ---------------------------------------------------------------------------------


def fmt(value, spec: str = ".2f", none: str = "-") -> str:
    return none if value is None else format(value, spec)


def pct(value) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def to_markdown(s: dict) -> str:
    meta = s["meta"]
    lines = [
        f"# Duplex run {s['run']}",
        "",
        f"- duration {s['duration_s']:.1f} s, git {str(meta.get('git_rev'))[:10]}, "
        f"LA {meta.get('link_adaptation')}, MCS {meta.get('mcs_index')}, "
        f"gain profile {meta.get('gain_profile')}, workers {meta.get('decode_workers')}",
        f"- loadavg at start: {meta.get('loadavg_at_start')}",
        "",
    ]
    for d, res in s["directions"].items():
        lines += [f"## {d}: {res['sender']} -> {res['receiver']}", ""]
        load = res["load"]
        if load:
            lines.append(
                f"udp_load: sent {load['sent']}, delivered {load['delivered']}, "
                f"lost {load['lost']} "
                f"({pct(load['loss_ratio'])}), corrupt {load['corrupt']}, goodput "
                f"{fmt(load['goodput_mbps'], '.3f')} Mb/s, latency p50/p99 "
                f"{fmt(load['latency_ms_p50'], '.0f')}/{fmt(load['latency_ms_p99'], '.0f')} ms"
            )
            lines.append("")
        lines += [
            "| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss "
            "| MCS min/med/max/final | MCS changes | SNR dB | offset dB |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for seg in res["segments"] + [{**res["overall"], "label": "all"}]:
            label = seg.get("label") or f"{seg['t0']:.0f}-{seg['t1']:.0f}"
            lines.append(
                f"| {label} | {fmt(seg['tx_gain_db'], '.1f')} | {fmt(seg['goodput_mbps'], '.3f')} "
                f"| {pct(seg['datagram_loss'])} | {pct(seg['subframe_loss'])} "
                f"| {fmt(seg['mcs_min'], '.0f')}/{fmt(seg['mcs_median'], '.1f')}"
                f"/{fmt(seg['mcs_max'], '.0f')}/{fmt(seg['mcs_final'], '.0f')} "
                f"| {seg['mcs_changes']} | {fmt(seg['snr_mean_db'], '.1f')} "
                f"| {fmt(seg['offset_db'], '.1f')} |"
            )
        lines.append("")
        if res["steps"]:
            lines += [
                "| gain step | at s | convergence s | frames lost in first 1 s |",
                "|---|---|---|---|",
            ]
            for st in res["steps"]:
                lines.append(
                    f"| {st['from_db']:g} -> {st['to_db']:g} dB | {st['t']:.0f} "
                    f"| {fmt(st['convergence_s'], '.0f')} | {st['missing_first_1s']:.0f} |"
                )
            lines.append("")
    lines += ["## RX overflow (events; start-up = first 3 s)", ""]
    for n, ov in s["rx_overflow"].items():
        lines.append(
            f"- node{n}: "
            + (
                "no data"
                if ov is None
                else f"loaded {ov['loaded']:.0f}, start-up {ov['startup']:.0f}, "
                f"total {ov['total']:.0f}"
            )
        )
    lines += ["", "## CPU (% of one core)", ""]
    cpu = s["cpu"] or {}
    for n in ("1", "2"):
        c = cpu.get(n)
        lines.append(
            f"- node{n}: "
            + (
                "no data"
                if not c
                else f"main {fmt(c['main_cpu_pct'], '.0f')}, workers {c['n_workers']} x "
                f"sum {fmt(c['worker_cpu_sum_pct'], '.0f')}, total {fmt(c['total_cpu_pct'], '.0f')}"
            )
        )
    return "\n".join(lines) + "\n"


def plot_run(s: dict, run: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    written = []
    for d, res in s["directions"].items():
        fig, axes = plt.subplots(4, 1, figsize=(9, 9), sharex=True)
        ax_mcs, ax_snr, ax_gp, ax_loss = axes
        traj = [(0.0, res["initial_mcs"])] if res["initial_mcs"] is not None else []
        traj += [(p["t"], p["mcs"]) for p in res["mcs_trajectory"]]
        if traj:
            ax_mcs.step(
                [t for t, _ in traj] + [s["duration_s"]],
                [m for _, m in traj] + [traj[-1][1]],
                where="post",
            )
        ax_mcs.set_ylabel("MCS index")
        fb = res["feedback"]
        ax_snr.plot(
            [f["t"] for f in fb if f["snr_f_db"] is not None],
            [f["snr_f_db"] for f in fb if f["snr_f_db"] is not None],
            label="SNR_f (dB)",
        )
        ax_snr.plot(
            [f["t"] for f in fb if f["offset_db"] is not None],
            [f["offset_db"] for f in fb if f["offset_db"] is not None],
            label="offset (dB)",
        )
        ax_snr.set_ylabel("dB")
        ax_snr.legend(loc="upper right")
        ax_gp.plot([b["t"] for b in res["bins"]], [b["goodput_mbps"] for b in res["bins"]])
        ax_gp.set_ylabel("goodput (Mb/s, 1 s)")
        ax_loss.plot(
            [b["t"] for b in res["bins"] if b["loss"] is not None],
            [b["loss"] * 100 for b in res["bins"] if b["loss"] is not None],
        )
        ax_loss.set_ylabel("subframe loss (%, 1 s)")
        ax_loss.set_xlabel("seconds from node ready")
        for ax in axes:
            for st in res["steps"]:
                ax.axvline(st["t"], color="red", linestyle="--", linewidth=0.8)
            ax.grid(alpha=0.3)
        fig.suptitle(f"{Path(s['run']).name}  {d}: {res['sender']} -> {res['receiver']}")
        fig.tight_layout()
        path = run / f"duplex_{d}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plots = "--no-plots" not in args
    runs = [Path(a) for a in args if not a.startswith("--")]
    if not runs or "-h" in args or "--help" in args:
        print(__doc__)
        return 0 if "-h" in args or "--help" in args else 2
    for run in runs:
        summary = summarize_run(run)
        (run / "summary.json").write_text(json.dumps(summary, indent=1))
        (run / "summary.md").write_text(to_markdown(summary))
        if plots:
            plot_run(summary, run)
        overall = {
            d: fmt(r["overall"]["goodput_mbps"], ".3f")
            + " Mb/s, loss "
            + pct(r["overall"]["subframe_loss"])
            for d, r in summary["directions"].items()
        }
        print(f"{run}: {overall}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

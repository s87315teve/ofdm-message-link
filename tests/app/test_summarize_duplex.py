"""summarize_duplex.py on small synthetic JSONL run directories (no hardware, no plots)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "summarize_duplex", ROOT / "experiments" / "duplex_fdd" / "summarize_duplex.py"
)
sd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sd)

MONO = 1000.0  # monotonic_s = elapsed_s + MONO


def stats_line(node, el, *, bytes_=0, decoded=0, missing=0, egress=0, ingress=0, mcs=8, ovf=0):
    return {
        "monotonic_s": el + MONO,
        "elapsed_s": el,
        "node_id": node,
        "tx": {"ingress_datagrams": ingress, "active_mcs": mcs},
        "rx": {
            "receiver": {
                "message_bytes_delivered": bytes_,
                "bursts_decoded": decoded,
                "missing_bursts": missing,
                "subframes_bad": 0,
            },
            "egress_datagrams": egress,
            "transport": {"rx_overflow_events": ovf},
        },
    }


def write(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def make_run(tmp_path: Path) -> Path:
    """90 s. Node 1 steps its gain at 30 s and 60 s; direction AB loses 50 % in 30-34 s."""
    run = tmp_path / "run"
    run.mkdir()
    meta = {
        "gain_profile": {"1": "0:-5,30:-20,60:-5", "2": None},
        "tx_gain_db": {"1": -5.0, "2": -5.0},
        "startup_s": 3.0,
        "load": {"AB": {"rate": 100}, "BA": {"rate": 0}},
    }
    (run / "meta.json").write_text(json.dumps(meta))
    n1, n2 = [], []
    decoded = missing = 0
    for k in range(0, 181):
        el = k / 2
        if k:
            lost = 5 if 30 < el <= 34 else 0  # per 0.5 s
            decoded += 5
            missing += lost
        # 100 datagrams/s offered, 10 B each delivered
        ingress = int(el * 100)
        n1.append(stats_line(1, el, ingress=ingress))
        n2.append(
            stats_line(
                2,
                el,
                bytes_=decoded * 900,
                decoded=decoded,
                missing=missing,
                egress=decoded,
                ovf=3 if el >= 1 else 0,
            )
        )
    write(run / "node1_stats.jsonl", n1)
    write(run / "node2_stats.jsonl", n2)
    la = [
        {"event": "la", "t": MONO + 10, "mode": "auto", "mcs_index": 10, "previous": 8},
        {"event": "la", "t": MONO + 31, "mode": "auto", "mcs_index": 8, "previous": 10},
        {"event": "la", "t": MONO + 35, "mode": "auto", "mcs_index": 9, "previous": 8},
        {
            "event": "feedback",
            "t": MONO + 5,
            "snr_mean_db": 14.0,
            "snr_f_db": 13.5,
            "offset_db": 1.0,
        },
        {
            "event": "feedback",
            "t": MONO + 40,
            "snr_mean_db": 6.0,
            "snr_f_db": 6.5,
            "offset_db": 2.0,
        },
        {"event": "mark", "t": MONO + 12, "text": "hello"},
    ]
    write(run / "la_1.jsonl", la)
    (run / "la_1.jsonl").write_text((run / "la_1.jsonl").read_text() + "{torn line\n")
    return run


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    return sd.summarize_run(make_run(tmp_path_factory.mktemp("duplex")))


def test_profile_and_steps():
    prof = sd.parse_profile("60:-5, 0:-5,30:-20")
    assert prof == [(0.0, -5.0), (30.0, -20.0), (60.0, -5.0)]
    assert sd.gain_steps(prof) == [
        {"t": 30.0, "from_db": -5.0, "to_db": -20.0},
        {"t": 60.0, "from_db": -20.0, "to_db": -5.0},
    ]
    assert sd.parse_profile(None) == []
    assert sd.gain_steps([(0.0, -5.0), (30.0, -5.0)]) == []


def test_segment_edges():
    prof = sd.parse_profile("0:-5,30:-20,60:-5")
    assert sd.segment_edges([prof, []], 90.0) == [0.0, 30.0, 60.0, 90.0]
    assert sd.segment_edges([[], []], 75.0) == [0.0, 30.0, 60.0, 75.0]


def test_read_jsonl_skips_torn_lines(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n{torn\n\n[1]\n{"a": 2}\n')
    assert [r["a"] for r in sd.read_jsonl(path)] == [1, 2]
    assert sd.read_jsonl(tmp_path / "missing.jsonl") == []


def test_segments_loss_and_goodput(summary):
    ab = summary["directions"]["AB"]
    segs = ab["segments"]
    assert [(s["t0"], s["t1"]) for s in segs] == [(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)]
    assert segs[0]["subframe_loss"] == pytest.approx(0.0)
    # 8 half-second chunks lost 5 frames each; 60 chunks decoded 5 each in the segment
    assert segs[1]["subframe_loss"] == pytest.approx(40 / 340)
    assert segs[0]["goodput_mbps"] == pytest.approx(10 * 900 * 8 / 1e6)  # 10 frames/s
    assert segs[0]["tx_gain_db"] == -5.0 and segs[1]["tx_gain_db"] == -20.0
    assert ab["overall"]["goodput_mbps"] > 0
    assert "BA" in summary["directions"]  # BA has stats but no load


def test_mcs_trajectory_and_snr(summary):
    ab = summary["directions"]["AB"]
    assert [(p["t"], p["mcs"]) for p in ab["mcs_trajectory"]] == [(10.0, 10), (31.0, 8), (35.0, 9)]
    assert ab["initial_mcs"] == 8
    seg0, seg1, _ = ab["segments"]
    assert seg0["mcs_min"] == 8 and seg0["mcs_max"] == 10 and seg0["mcs_changes"] == 1
    assert seg1["mcs_final"] == 9 and seg1["mcs_changes"] == 2
    assert seg0["snr_mean_db"] == pytest.approx(14.0)
    assert seg1["snr_mean_db"] == pytest.approx(6.0)
    assert seg1["offset_db"] == pytest.approx(2.0)
    assert ab["marks"] == [{"t": 12.0, "text": "hello"}]


def test_convergence_after_step(summary):
    steps = summary["directions"]["AB"]["steps"]
    assert [s["t"] for s in steps] == [30.0, 60.0]
    # loss until 34 s, MCS changes at 31 s and 35 s: first window after the last bad one is 36 s
    assert steps[0]["convergence_s"] == pytest.approx(6.0)
    assert steps[0]["missing_first_1s"] == 10
    assert steps[1]["convergence_s"] == pytest.approx(0.0)


def test_convergence_none_when_never_good():
    rx = sd.Series(
        [0.0, 1.0, 2.0, 3.0, 4.0],
        {
            "missing": [0, 5, 10, 15, 20],
            "decoded": [0, 5, 10, 15, 20],
        },
    )
    assert sd.convergence(rx, [], 0.0, 4.0, loaded=True) is None
    assert sd.convergence(rx, [], 0.0, 4.0, loaded=False) is None


def test_overflow_excludes_startup(summary):
    ov = summary["rx_overflow"]["2"]
    assert ov["startup"] == 3 and ov["loaded"] == 0 and ov["total"] == 3


def test_markdown_renders(summary):
    text = sd.to_markdown(summary)
    assert "## AB: node1 -> node2" in text and "convergence s" in text

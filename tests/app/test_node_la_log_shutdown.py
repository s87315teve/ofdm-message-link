"""The engine child writes the LA log's queued tail before it exits.

The child's LA log is written by a thread of its own (a slow disk must not
stop TX or RX).  With every write slowed down, lines are still queued when
the stop command arrives; ``_node_main`` must write them all (in order)
before the process exits, which would otherwise kill the daemon writer.
"""

from __future__ import annotations

import json

from mcs_switch_helpers import free_udp_ports
from slow_la_log_engine import WRITE_DELAY_S, node_main_with_slow_log_writes

from ofdm_message_link import node_options
from ofdm_message_link.link import build_phy_profile
from ofdm_message_link.node_engine import NodeSpec, ProcessNodeWorker
from ofdm_message_link.options import load_link_config

MARKS = 100


def test_stopping_the_engine_writes_the_queued_la_log_tail(tmp_path):
    config = load_link_config("configs/default.yaml", [node_options.DEFAULT_NODE_OVERLAY])
    profile = build_phy_profile(config)
    ingress, egress = free_udp_ports(2)
    la_log = tmp_path / "la_1.jsonl"
    spec = NodeSpec(
        node_id=1,
        peer_id=2,
        mcs_index=8,
        control_mcs_index=8,
        seed_range=node_options.SEED_RANGES[1],
        ingress_port=ingress,
        egress=("127.0.0.1", egress),
        la_spec="fixed",
        la_log_path=str(la_log),
    )
    worker = ProcessNodeWorker(profile, spec, lambda kind, value: None, decode_workers=1,
                               target=node_main_with_slow_log_writes)
    worker.start()
    try:
        for number in range(MARKS):  # ~MARKS * WRITE_DELAY_S of writing, queued at once
            worker.mark(f"m{number}")
    finally:
        worker.stop()  # the stop command follows the marks on the control queue
    assert MARKS * WRITE_DELAY_S > 0.2  # the queue is far from written when "stop" lands
    rows = [json.loads(line) for line in la_log.read_text().splitlines()]
    marks = [row["text"] for row in rows if row["event"] == "mark"]
    assert marks == [f"m{number}" for number in range(MARKS)]
    assert rows[-1] == {"event": "mark", "t": rows[-1]["t"], "text": f"m{MARKS - 1}"}

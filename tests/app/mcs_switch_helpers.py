"""Helpers shared by the runtime MCS-switch regression guards.

``every_transition`` builds an MCS schedule whose consecutive pairs are every
ordered pair of distinct indices exactly once, so one pass covers every
adjacent switch (n -> n+-1) and every cross-level jump (8 <-> 17, 9 <-> 15,
...).  ``free_udp_ports`` hands out ephemeral loopback ports outside the
52000-52999 range the B210 measurement scripts use.
``assert_sent_joins_received`` checks a node's ``--events`` ``sent`` lines
against its peer's ``burst`` lines.
"""

from __future__ import annotations

import itertools
import socket
from collections.abc import Sequence

# runs/jetson_b210_*/scripts/common.sh binds ports in this range (52001,
# 52002, 52031, 52211, ...); a test must never take one of them.
_RESERVED_PORTS = range(52000, 53000)


def every_transition(indices: Sequence[int]) -> list[int]:
    """A closed walk over ``indices`` that takes every ordered pair (a, b), a != b, once.

    The complete directed graph is Eulerian (in-degree equals out-degree), so
    Hierholzer's algorithm finds the walk; it has ``n * (n - 1) + 1`` entries
    and starts and ends at ``indices[0]``.
    """

    nodes = list(indices)
    if len(nodes) < 2:
        return nodes
    unused = {node: [other for other in reversed(nodes) if other != node] for node in nodes}
    stack = [nodes[0]]
    walk: list[int] = []
    while stack:
        node = stack[-1]
        if unused[node]:
            stack.append(unused[node].pop())
        else:
            walk.append(stack.pop())
    walk.reverse()
    assert len(walk) == len(nodes) * (len(nodes) - 1) + 1
    return walk


def transitions(sequence: Sequence[int]) -> set[tuple[int, int]]:
    """The distinct (previous, next) pairs where ``sequence`` changes value."""

    return {(a, b) for a, b in itertools.pairwise(sequence) if a != b}


def required_transitions(indices: Sequence[int]) -> set[tuple[int, int]]:
    return {(a, b) for a in indices for b in indices if a != b}


def free_udp_ports(count: int) -> list[int]:
    """``count`` distinct loopback UDP ports the OS reports free, never 52000-52999."""

    held: list[socket.socket] = []
    ports: list[int] = []
    try:
        while len(ports) < count:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            held.append(probe)
            port = probe.getsockname()[1]
            if port not in _RESERVED_PORTS:
                ports.append(port)
    finally:
        for probe in held:
            probe.close()
    return ports


def assert_sent_joins_received(sent_rows, rx_rows, datagrams, *, waits: bool):
    """Node 1's ``sent`` lines against node 2's ``burst`` lines of the same run.

    One ``sent`` line per accepted burst (``n`` = 1, 2, ...), each accepted at
    the first attempt; the data bursts' ``first_sequence`` / ``subframes``
    tile 0 .. ``datagrams`` - 1 in order; node 2 decoded every one of them
    exactly once, whole, at the MCS the ``sent`` line names.  ``waits``: the
    aggregation limits make the fill step wait (node_app's 4 ms minimum burst
    interval), so ``planned_mcs`` is set; else it is None.  Returns the data
    lines.
    """

    assert [row["n"] for row in sent_rows] == list(range(1, len(sent_rows) + 1))
    assert all(row["attempt"] == 1 for row in sent_rows)
    monos = [row["mono"] for row in sent_rows]
    assert monos == sorted(monos)
    data = [row for row in sent_rows if not row["control_only"]]
    assert all(row["first_sequence"] is None and row["subframes"] == 0
               for row in sent_rows if row["control_only"])
    expected = 0
    for row in data:
        assert row["first_sequence"] == expected % 65536 and row["subframes"] >= 1
        expected += row["subframes"]
    assert expected == datagrams
    received = [row for row in rx_rows if row["kind"] == "data"]
    by_seq = {row["seq"]: row for row in received}
    assert len(by_seq) == len(received) == len(data)  # each burst decoded once
    for row in data:
        rx = by_seq[row["first_sequence"]]
        # sf_ok counts the CONTROL subframes in front of the data too.
        whole = row["subframes"] + row["control_subframes"]
        assert (rx["mcs"], rx["sf_ok"], rx["sf_bad"]) == (row["mcs"], whole, 0)
        assert rx["pl"] == "ok" and rx["fcs"] is True
    # planned_mcs: the MCS the aggregation wait sized the burst with; with a
    # minimum burst interval every data burst after the first accepted one
    # waited (the first had no previous burst to wait after).
    assert all("planned_mcs" in row for row in sent_rows)
    if waits:
        assert all(isinstance(row["planned_mcs"], int) for row in data if row["n"] > 1)
    else:
        assert all(row["planned_mcs"] is None for row in sent_rows)
    return data

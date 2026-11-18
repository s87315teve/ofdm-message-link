"""Helpers shared by the runtime MCS-switch regression guards.

``every_transition`` builds an MCS schedule whose consecutive pairs are every
ordered pair of distinct indices exactly once, so one pass covers every
adjacent switch (n -> n+-1) and every cross-level jump (8 <-> 17, 9 <-> 15,
...).  ``free_udp_ports`` hands out ephemeral loopback ports outside the
52000-52999 range the B210 measurement scripts use.
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

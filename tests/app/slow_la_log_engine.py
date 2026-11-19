"""An engine child whose log writes are slow: the queue is still full at shutdown.

``ProcessNodeWorker(..., target=node_main_with_slow_log_writes)`` runs the real
``node_engine._node_main`` with every :class:`BackgroundLineWriter` write
taking ``WRITE_DELAY_S`` longer, as on a stalling SD card.  A module of its
own so the spawned child can import it.
"""

from __future__ import annotations

import time

WRITE_DELAY_S = 0.005


def node_main_with_slow_log_writes(*args) -> None:
    from ofdm_message_link import line_writer
    from ofdm_message_link.node_engine import _node_main

    write = line_writer.BackgroundLineWriter._write

    def slow_write(self, text):
        time.sleep(WRITE_DELAY_S)
        return write(self, text)

    line_writer.BackgroundLineWriter._write = slow_write
    _node_main(*args)

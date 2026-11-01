"""Fixed MCS: the link adaptation that never adapts (``--link-adaptation fixed``)."""

from __future__ import annotations

from collections.abc import Mapping

from ofdm_link.phy.mcs_table import McsTableError, mcs_entry

from .api import Decision, FeedbackReport, LaContext, SentBurst


class FixedMcs:
    """Always returns one MCS; ``set_mcs`` changes it (GUI combo, ``set_mcs``).

    ``mcs_index`` defaults to ``context.initial_mcs_index`` and may be any
    transmit-visible v5 entry, also one the ladder leaves out.
    """

    def __init__(self, context: LaContext, mcs_index: int | None = None) -> None:
        self._mcs = self._checked(
            context.initial_mcs_index if mcs_index is None else mcs_index
        )

    @staticmethod
    def _checked(index: object) -> int:
        try:
            entry = mcs_entry(index)  # type: ignore[arg-type]
        except McsTableError as exc:
            raise ValueError(f"mcs_index {index!r}: {exc}") from exc
        if entry.family != "v5" or not entry.tx_visible:
            raise ValueError(f"mcs_index {index} is not a transmit-visible v5 entry")
        return entry.index

    @property
    def mcs_index(self) -> int:
        return self._mcs

    def set_mcs(self, mcs_index: int) -> None:
        self._mcs = self._checked(mcs_index)

    def on_sent(self, burst: SentBurst) -> None:
        pass

    def on_feedback(self, report: FeedbackReport) -> None:
        pass

    def decide(self, now_s: float) -> Decision:
        return Decision(self._mcs, reason="fixed")

    def snapshot(self) -> Mapping[str, object]:
        return {"algorithm": "fixed", "mcs_index": self._mcs}


__all__ = ["FixedMcs"]

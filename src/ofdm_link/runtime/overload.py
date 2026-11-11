"""Opt-in receive overload protection.  Off by default; JMS1 measurement runs keep it off.

On a machine that cannot keep up (two slow cores, 5 MS/s at high airtime)
the receive owner waits on a full decode pool, the radio overflows, and
failed Turbo blocks, which run all 8 iterations at about 4 times the cost
of a good one, make the backlog worse: the loss curve falls off a cliff.
Two switches trade that cliff for a slope:

``drop``
    When the decode pool is full, the owner drops the segment it cannot
    queue instead of waiting.  Counted in the pool's ``dropped_full``.
``give-up``
    The Turbo decoder stops a code block whose hard decisions still change
    in more than 1/64 of its bits at iteration 4 (``turbo.DEFAULT_GIVE_UP_AFTER``)
    instead of running all 8.  A PSDU with such a block is counted in the
    decoder's ``payload_abandoned``, not in ``crc_failures``,
    ``psdu_fcs_failures`` or ``payload_decode_attempts``.

Both change what a lost burst means, which is why each has its own counter
and why neither may be on in a run that measures loss, BLER, SNR or EVM:
ordinary loss and BLER keep their definitions only with protection off.
``both`` turns on the two.  The apps' ``--overload-protection`` sets
``OFDM_LINK_OVERLOAD_PROTECTION`` before any child starts, and the
receiver reads it from there when it is not given a setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MODES = ("off", "drop", "give-up", "both")
ENVIRONMENT = "OFDM_LINK_OVERLOAD_PROTECTION"


@dataclass(frozen=True, slots=True)
class OverloadProtection:
    drop_when_full: bool = False
    give_up_after: int = 0

    @property
    def enabled(self) -> bool:
        return self.drop_when_full or self.give_up_after > 0

    @property
    def mode(self) -> str:
        if self.drop_when_full and self.give_up_after:
            return "both"
        if self.drop_when_full:
            return "drop"
        return "give-up" if self.give_up_after else "off"

    @classmethod
    def from_mode(cls, mode: str) -> OverloadProtection:
        from ofdm_link.phy.turbo import DEFAULT_GIVE_UP_AFTER

        if mode not in MODES:
            raise ValueError(f"overload protection must be one of {MODES}, not {mode!r}")
        return cls(
            drop_when_full=mode in ("drop", "both"),
            give_up_after=DEFAULT_GIVE_UP_AFTER if mode in ("give-up", "both") else 0,
        )


OFF = OverloadProtection()


def from_environment() -> OverloadProtection:
    """The setting ``--overload-protection`` left for this process; off when unset."""

    value = os.environ.get(ENVIRONMENT, "").strip().lower()
    return OFF if value in ("", "off") else OverloadProtection.from_mode(value)


def request(mode: str) -> OverloadProtection:
    """Record ``mode`` for this process and every child it starts from now on."""

    protection = OverloadProtection.from_mode(mode)
    os.environ[ENVIRONMENT] = protection.mode
    return protection

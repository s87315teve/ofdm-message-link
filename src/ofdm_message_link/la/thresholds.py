"""The SNR threshold table and the MCS ladder built from it.

One YAML file (``configs/la/thresholds_pluto_v5.yaml``) holds the preamble
SNR each MCS needs for < 1 % loss.  The GUI lights read it through
``dashboard.SNR_THRESHOLD_DB``; the link adaptation builds its ladder from
it with :func:`build_ladder`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

from ofdm_link.phy.mcs_table import (
    ALL_MCS_ENTRIES,
    McsTableEntry,
    McsTableError,
    mcs_entry,
    v5_entries,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_THRESHOLDS_PATH = REPO_ROOT / "configs" / "la" / "thresholds_pluto_v5.yaml"
_TABLE_KEY = "snr_threshold_db"
_KNOWN_INDICES = frozenset(entry.index for entry in ALL_MCS_ENTRIES)


class ThresholdsError(ValueError):
    """Raised when a threshold file is missing or malformed."""


def load_thresholds(path: str | Path | None = None) -> dict[int, float]:
    """Return ``{mcs_index: snr_db}`` from a thresholds YAML file.

    ``path`` defaults to :data:`DEFAULT_THRESHOLDS_PATH`.  The file holds one
    mapping ``snr_threshold_db: {index: dB, ...}``; indices must name entries
    of the MCS table and values must be finite numbers.
    """

    source = Path(DEFAULT_THRESHOLDS_PATH if path is None else path)
    try:
        with source.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except OSError as exc:
        raise ThresholdsError(f"cannot read thresholds file {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ThresholdsError(f"thresholds file {source} is not valid YAML: {exc}") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get(_TABLE_KEY), Mapping):
        raise ThresholdsError(f"thresholds file {source} needs a mapping '{_TABLE_KEY}'")
    return parse_thresholds(document[_TABLE_KEY], source=str(source))


def parse_thresholds(table: Mapping[object, object], *, source: str = "table") -> dict[int, float]:
    """Validate one ``{index: dB}`` mapping; keys sorted ascending."""

    parsed: dict[int, float] = {}
    for key, value in table.items():
        if type(key) is not int or key not in _KNOWN_INDICES:
            raise ThresholdsError(f"{source}: '{key}' is not an MCS index")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ThresholdsError(f"{source}: MCS {key} threshold '{value}' is not a number")
        if not math.isfinite(value):
            raise ThresholdsError(f"{source}: MCS {key} threshold must be finite")
        parsed[key] = float(value)
    return dict(sorted(parsed.items()))


def build_ladder(
    thresholds: Mapping[int, float],
    entries: Iterable[McsTableEntry] | None = None,
) -> tuple[McsTableEntry, ...]:
    """Return the usable rungs in ascending PHY rate (and threshold).

    ``entries`` defaults to the transmit-visible v5 entries.  An entry with
    no threshold is left out.  The rest are walked in increasing threshold
    order (a tie goes to the faster entry first) and an entry is kept only if
    it is faster than every entry kept before it: an entry that needs at
    least the SNR of a faster one is dominated and never worth sending.
    """

    candidates = [
        entry
        for entry in (v5_entries() if entries is None else entries)
        if entry.index in thresholds
    ]
    candidates.sort(
        key=lambda e: (thresholds[e.index], -e.spectral_efficiency, e.index)
    )
    ladder: list[McsTableEntry] = []
    for entry in candidates:
        if not ladder or entry.spectral_efficiency > ladder[-1].spectral_efficiency:
            ladder.append(entry)
    return tuple(ladder)


def rung_at_or_below(ladder: tuple[McsTableEntry, ...], mcs_index: int) -> McsTableEntry:
    """The fastest rung whose spectral efficiency does not exceed ``mcs_index``'s.

    Used to map an index that is not on the ladder (a dominated or unknown
    entry) to a rung; the lowest rung if none is slower.
    """

    if not ladder:
        raise ThresholdsError("the MCS ladder is empty")
    for entry in ladder:
        if entry.index == mcs_index:
            return entry
    try:
        efficiency = mcs_entry(mcs_index).spectral_efficiency
    except McsTableError:
        return ladder[0]
    slower = [entry for entry in ladder if entry.spectral_efficiency <= efficiency]
    return slower[-1] if slower else ladder[0]


__all__ = [
    "DEFAULT_THRESHOLDS_PATH",
    "ThresholdsError",
    "build_ladder",
    "load_thresholds",
    "parse_thresholds",
    "rung_at_or_below",
]

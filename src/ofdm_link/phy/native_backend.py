"""Which implementation each native-accelerated piece uses: ``auto``, ``python`` or ``native``.

Every piece keeps its Python (or, for Turbo, its original double-precision)
implementation as the reference and the fallback.  The choice is read from
the environment, so it reaches the engine child and every decode worker:

``OFDM_LINK_NATIVE``
    ``auto`` (default), ``python`` or ``native`` for every piece.  ``python``
    here forces every piece to its reference implementation, whatever the
    per-piece variables say.
``OFDM_LINK_NATIVE_<PIECE>``
    The same for one piece: ``ACQ`` (preamble acquisition), ``HEADER`` (the
    v5 header probe), ``TURBO`` (the Turbo decoder kernel) and ``TX`` (rate
    matching, mapping and OFDM modulation on transmit).

``auto`` uses the native module when it imports and falls back with one
warning when it does not; ``native`` makes a missing module an error.  The
apps' ``--native-backend`` sets ``OFDM_LINK_NATIVE`` before any child starts.
"""

from __future__ import annotations

import importlib
import os
import sys
import warnings
from types import ModuleType

PIECES = {
    "acq": "ofdm_link.phy._acq_native",
    "header": "ofdm_link.phy._header_native",
    "turbo": "ofdm_link.phy._turbo_native",
    "tx": "ofdm_link.phy._tx_native",
}
CHOICES = ("auto", "python", "native")
GLOBAL_ENV = "OFDM_LINK_NATIVE"

_resolved: dict[str, ModuleType | None] = {}
_warned: set[str] = set()


class NativeBackendError(RuntimeError):
    """Raised when ``native`` is required but the module cannot be imported."""


def env_name(piece: str) -> str:
    _check_piece(piece)
    return f"{GLOBAL_ENV}_{piece.upper()}"


def requested(piece: str) -> str:
    """The choice in force for ``piece``: the global ``python`` wins, then the piece's own."""

    _check_piece(piece)
    overall = _read(GLOBAL_ENV)
    if overall == "python":
        return "python"
    own = _read(env_name(piece))
    return own or overall or "auto"


def module(piece: str) -> ModuleType | None:
    """The native module to use for ``piece``, or None for the reference implementation."""

    if piece in _resolved:
        return _resolved[piece]
    choice = requested(piece)
    loaded: ModuleType | None = None
    if choice != "python":
        try:
            loaded = importlib.import_module(PIECES[piece])
        except ImportError as error:
            if choice == "native":
                raise NativeBackendError(
                    f"{env_name(piece)}=native but {PIECES[piece]} does not import: {error}; "
                    "rebuild it with: python setup.py build_ext --inplace"
                ) from error
            if piece not in _warned:
                _warned.add(piece)
                warnings.warn(
                    f"native {piece} module {PIECES[piece]} is not built ({error}); "
                    "using the Python implementation",
                    RuntimeWarning,
                    stacklevel=2,
                )
    _resolved[piece] = loaded
    return loaded


def uses_native(piece: str) -> bool:
    return module(piece) is not None


def set_choice(value: str, piece: str | None = None) -> None:
    """Set the choice for one piece, or for all with ``piece=None``; children inherit it."""

    if value not in CHOICES:
        raise ValueError(f"backend must be one of {CHOICES}, not {value!r}")
    os.environ[GLOBAL_ENV if piece is None else env_name(piece)] = value
    reset()


def reset() -> None:
    """Forget resolved choices, so the next call reads the environment again."""

    _resolved.clear()


def describe() -> dict[str, str]:
    """``{piece: "native" | "python"}`` as resolved now; for logs and stats."""

    return {piece: ("native" if uses_native(piece) else "python") for piece in PIECES}


def _read(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    value = value.strip().lower()
    if value not in CHOICES:
        print(f"[native-backend] ignoring {name}={value!r}; use one of {CHOICES}",
              file=sys.stderr, flush=True)
        return None
    return value


def _check_piece(piece: str) -> None:
    if piece not in PIECES:
        raise ValueError(f"unknown native piece {piece!r}; one of {tuple(PIECES)}")

"""Load link adaptation and reporter classes from a command-line spec.

``SPEC`` is a built-in name, an importable ``package.module:Class`` or a
file ``path/to/file.py:Class``.  ``key=value`` arguments become constructor
keyword arguments: the value is a Python literal if it parses as one, else a
string.  The class is built as ``Class(context, **kwargs)`` and the result
must satisfy :class:`~.api.LinkAdaptation` or :class:`~.api.FeedbackReporter`.

Every error ends in a one-line :class:`SystemExit`, so a typo in a spec stops
the program with a readable message instead of a traceback.  Only the spec
strings cross a process boundary; each process builds its own instance.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from .api import FeedbackReporter, LaContext, LinkAdaptation, ReporterContext

Kind = Literal["la", "reporter"]

BUILTINS: Mapping[str, Mapping[str, str]] = {
    "la": {
        "fixed": "ofdm_message_link.la.fixed:FixedMcs",
        "olla": "ofdm_message_link.la.olla:OllaLinkAdaptation",
        "arf": "ofdm_message_link.la.examples:ArfLinkAdaptation",
    },
    "reporter": {
        "default": "ofdm_message_link.la.reporter:DefaultReporter",
    },
}
_PROTOCOLS = {"la": LinkAdaptation, "reporter": FeedbackReporter}
_FLAGS = {"la": "--link-adaptation", "reporter": "--feedback-reporter"}


def _fail(kind: Kind, message: str) -> SystemExit:
    return SystemExit(f"{_FLAGS[kind]}: {message}")


def _load_file(path: Path, kind: Kind):
    if not path.is_file():
        raise _fail(kind, f"no such file: {path}")
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]
    name = f"_ofdm_la_user_{path.stem}_{digest}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise _fail(kind, f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and pickling look the module up by name
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        raise _fail(kind, f"importing {path} failed: {type(exc).__name__}: {exc}") from exc
    return module


def load(spec: str, kind: Kind) -> type:
    """Return the class a spec names (built-in, ``module:Class`` or ``file.py:Class``)."""

    if kind not in BUILTINS:
        raise ValueError(f"kind must be one of {sorted(BUILTINS)}")
    text = spec.strip()
    text = BUILTINS[kind].get(text, text)
    target, colon, class_name = text.rpartition(":")
    if not colon or not target or not class_name:
        names = ", ".join(sorted(BUILTINS[kind]))
        raise _fail(
            kind,
            f"'{spec}' is not a built-in ({names}), 'package.module:Class' or 'file.py:Class'",
        )
    if target.endswith(".py") or "/" in target or "\\" in target:
        module = _load_file(Path(target).expanduser(), kind)
    else:
        try:
            module = importlib.import_module(target)
        except Exception as exc:
            raise _fail(
                kind, f"cannot import module '{target}': {type(exc).__name__}: {exc}"
            ) from exc
    cls = getattr(module, class_name, None)
    if cls is None:
        raise _fail(kind, f"'{target}' has no attribute '{class_name}'")
    if not isinstance(cls, type):
        raise _fail(kind, f"'{class_name}' in '{target}' is not a class")
    return cls


def parse_kwargs(pairs: Iterable[str] | None, kind: Kind = "la") -> dict[str, object]:
    """Turn ``["k=v", ...]`` into keyword arguments (literal if possible, else str)."""

    kwargs: dict[str, object] = {}
    for pair in pairs or ():
        key, equals, raw = pair.partition("=")
        key = key.strip()
        if not equals:
            raise _fail(kind, f"argument '{pair}' is not key=value")
        if not key.isidentifier():
            raise _fail(kind, f"argument key '{key}' is not a valid name")
        if key in kwargs:
            raise _fail(kind, f"argument '{key}' is given twice")
        try:
            value: object = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            value = raw.strip()
        kwargs[key] = value
    return kwargs


def build(
    spec: str,
    kind: Kind,
    context: LaContext | ReporterContext,
    args: Iterable[str] | None = None,
):
    """Load, construct with ``Class(context, **kwargs)`` and check the Protocol."""

    cls = load(spec, kind)
    kwargs = parse_kwargs(args, kind)
    try:
        instance = cls(context, **kwargs)
    except SystemExit:
        raise
    except Exception as exc:
        raise _fail(
            kind, f"{cls.__name__}({', '.join(kwargs)}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    protocol = _PROTOCOLS[kind]
    if not isinstance(instance, protocol):
        missing = [
            name
            for name in sorted(getattr(protocol, "__protocol_attrs__", ()))
            if not hasattr(instance, name)
        ]
        detail = f" (missing: {', '.join(missing)})" if missing else ""
        raise _fail(kind, f"{cls.__name__} does not implement {protocol.__name__}{detail}")
    return instance


def build_link_adaptation(
    spec: str, context: LaContext, args: Iterable[str] | None = None
) -> LinkAdaptation:
    """``--link-adaptation SPEC --link-adaptation-arg k=v ...`` → instance."""

    return build(spec, "la", context, args)


def build_reporter(
    spec: str, context: ReporterContext, args: Iterable[str] | None = None
) -> FeedbackReporter:
    """``--feedback-reporter SPEC --feedback-reporter-arg k=v ...`` → instance."""

    return build(spec, "reporter", context, args)


__all__ = [
    "BUILTINS",
    "build",
    "build_link_adaptation",
    "build_reporter",
    "load",
    "parse_kwargs",
]

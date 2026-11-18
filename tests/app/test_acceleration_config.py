"""acceleration.cpu_cores and acceleration.decode_workers reach the apps.

Precedence is the command-line flag, then the configuration (base plus
overlays), then each app's built-in default.  The built-in defaults are what
every app used before the keys took effect: one NumPy/BLAS thread per process,
rx_app 4 decode workers and node_app 3.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ofdm_link.config import ConfigError, load_config
from ofdm_link.runtime import cpu_budget
from ofdm_message_link import node_app, node_options, options, rx_app, tx_app

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = str(ROOT / "configs" / "default.yaml")
ONE_WAY = str(ROOT / "configs" / "profiles" / "ota_2p45ghz.yaml")
DUPLEX = str(ROOT / "configs" / "profiles" / "duplex_fdd_2420_2460.yaml")


def _overlay(tmp_path: Path, **acceleration) -> str:
    path = tmp_path / "acceleration.yaml"
    path.write_text(yaml.safe_dump({"acceleration": acceleration}), encoding="utf-8")
    return str(path)


def _base_without_keys(tmp_path: Path) -> str:
    raw = yaml.safe_load(Path(DEFAULT).read_text(encoding="utf-8"))
    del raw["acceleration"]["decode_workers"], raw["acceleration"]["cpu_cores"]
    path = tmp_path / "base.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def _rx(*argv: str):
    args = rx_app.build_parser().parse_args(["--config", DEFAULT, *argv])
    resolved = options.resolve(args)
    workers = options.resolve_decode_workers(args, resolved.config, rx_app.DEFAULT_DECODE_WORKERS)
    assert args.decode_workers == workers
    return resolved.cpu_cores, workers


def _node(*argv: str):
    args = node_options.parse_args(["--node-id", "1", "--headless", "--config", DEFAULT, *argv])
    setup = node_options.resolve_node(args)
    return setup.resolved.cpu_cores, args.decode_workers


def _tx(*argv: str) -> int:
    args = tx_app.build_parser().parse_args(["--config", DEFAULT, *argv])
    return options.resolve(args).cpu_cores


@pytest.fixture
def recorded_budget(monkeypatch):
    """Record budgets instead of changing this test process's thread pools."""

    applied: list[int] = []
    monkeypatch.delenv(cpu_budget.ENVIRONMENT, raising=False)
    monkeypatch.setattr(cpu_budget, "apply_cpu_budget", applied.append)
    return applied


# --------------------------------------------------------------------------
# Defaults: nothing changes unless someone sets a key or a flag
# --------------------------------------------------------------------------


def test_default_yaml_leaves_both_keys_to_the_built_in_defaults():
    acceleration = load_config(DEFAULT).acceleration
    assert acceleration.decode_workers is None
    assert acceleration.cpu_cores == cpu_budget.DEFAULT_CPU_CORES == 1


def test_every_app_keeps_its_previous_defaults():
    assert _rx() == (1, 4)
    assert _node() == (1, 3)
    assert _tx() == 1


def test_a_configuration_without_the_keys_gets_the_same_defaults(tmp_path):
    base = _base_without_keys(tmp_path)
    acceleration = load_config(base).acceleration
    assert (acceleration.decode_workers, acceleration.cpu_cores) == (None, None)
    assert _rx("--config", base) == (1, 4)
    assert _node("--config", base) == (1, 3)
    assert _tx("--config", base) == 1


def test_the_engine_budget_defaults_to_one_thread(monkeypatch):
    monkeypatch.delenv(cpu_budget.ENVIRONMENT, raising=False)
    assert cpu_budget.requested() == 1
    monkeypatch.setenv(cpu_budget.ENVIRONMENT, "not a number")
    assert cpu_budget.requested() == 1


# --------------------------------------------------------------------------
# Precedence: flag > configuration > built-in default
# --------------------------------------------------------------------------


def test_the_configuration_overrides_the_built_in_defaults(tmp_path):
    overlay = _overlay(tmp_path, decode_workers=6, cpu_cores=2)
    assert _rx("--overlay", ONE_WAY, "--overlay", overlay) == (2, 6)
    assert _node("--overlay", DUPLEX, "--overlay", overlay) == (2, 6)
    assert _tx("--overlay", ONE_WAY, "--overlay", overlay) == 2


def test_cpu_cores_zero_is_kept_for_the_affinity_lookup(tmp_path):
    overlay = _overlay(tmp_path, cpu_cores=0)
    assert _tx("--overlay", ONE_WAY, "--overlay", overlay) == 0
    assert cpu_budget.resolve_cpu_cores(0) >= 1


def test_the_flags_override_the_configuration(tmp_path):
    overlay = _overlay(tmp_path, decode_workers=6, cpu_cores=2)
    flags = ("--decode-workers", "2", "--cpu-cores", "3")
    assert _rx("--overlay", ONE_WAY, "--overlay", overlay, *flags) == (3, 2)
    assert _node("--overlay", DUPLEX, "--overlay", overlay, *flags) == (3, 2)
    assert _tx("--overlay", ONE_WAY, "--overlay", overlay, "--cpu-cores", "3") == 3


def test_flags_without_any_configuration_value(tmp_path):
    base = _base_without_keys(tmp_path)
    assert _rx("--config", base, "--decode-workers", "1", "--cpu-cores", "2") == (2, 1)
    assert _node("--config", base, "--decode-workers", "5") == (1, 5)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "acceleration",
    [
        {"decode_workers": 0},
        {"decode_workers": -1},
        {"decode_workers": 65},
        {"decode_workers": 2.5},
        {"decode_workers": "4"},
        {"decode_workers": True},
        {"cpu_cores": -1},
        {"cpu_cores": 257},
        {"cpu_cores": 1.5},
        {"cpu_cores": "2"},
        {"cpu_cores": True},
    ],
)
def test_invalid_configuration_values_are_rejected(tmp_path, acceleration):
    with pytest.raises(ConfigError, match="acceleration"):
        load_config(DEFAULT, [_overlay(tmp_path, **acceleration)])


def test_the_bounds_are_inclusive(tmp_path):
    acceleration = load_config(
        DEFAULT, [_overlay(tmp_path, decode_workers=64, cpu_cores=256)]
    ).acceleration
    assert (acceleration.decode_workers, acceleration.cpu_cores) == (64, 256)


@pytest.mark.parametrize(
    "argv",
    [
        ("--decode-workers", "0"),
        ("--decode-workers", "65"),
        ("--cpu-cores", "-1"),
        ("--cpu-cores", "257"),
        ("--cpu-cores", "two"),
    ],
)
def test_invalid_flags_are_rejected(argv):
    with pytest.raises(SystemExit):
        rx_app.build_parser().parse_args(list(argv))
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", *argv])


def test_tx_app_takes_cpu_cores_but_has_no_decode_workers():
    with pytest.raises(SystemExit):
        tx_app.build_parser().parse_args(["--cpu-cores", "-1"])
    with pytest.raises(SystemExit):
        tx_app.build_parser().parse_args(["--decode-workers", "2"])


# --------------------------------------------------------------------------
# The resolved values reach the decode pool and the engine processes
# --------------------------------------------------------------------------


class _Reached(Exception):
    pass


def test_rx_app_main_hands_the_resolved_count_to_the_window(tmp_path, monkeypatch,
                                                            recorded_budget):
    seen = {}

    def window(resolved, egress, **kwargs):
        seen.update(kwargs)
        raise _Reached

    monkeypatch.setattr(rx_app, "ReceiveWindow", window)
    monkeypatch.setattr(rx_app.qt_runtime, "create_application", lambda: None)
    from ofdm_link.runtime import allocator

    monkeypatch.setattr(allocator, "keep_freed_memory", lambda: None)
    overlay = _overlay(tmp_path, decode_workers=2, cpu_cores=3)
    with pytest.raises(_Reached):
        rx_app.main(["--config", DEFAULT, "--overlay", ONE_WAY, "--overlay", overlay])
    assert seen["decode_workers"] == 2
    assert recorded_budget == [3]
    # The engine child reads the same budget from the environment it inherits.
    assert cpu_budget.requested() == 3


def test_node_app_main_hands_the_resolved_count_to_the_worker(tmp_path, monkeypatch,
                                                             recorded_budget):
    seen = {}

    def worker(profile, spec, listener, **kwargs):
        seen.update(kwargs)
        raise _Reached

    monkeypatch.setattr(node_app, "ProcessNodeWorker", worker)
    overlay = _overlay(tmp_path, decode_workers=5, cpu_cores=2)
    with pytest.raises(_Reached):
        node_app.main(["--node-id", "1", "--headless", "--config", DEFAULT,
                       "--overlay", DUPLEX, "--overlay", overlay, "--decode-workers", "2"])
    assert seen["decode_workers"] == 2
    assert recorded_budget == [2]
    assert cpu_budget.requested() == 2


def test_tx_app_main_applies_the_configured_budget(tmp_path, monkeypatch, recorded_budget):
    def window(*args, **kwargs):
        raise _Reached

    monkeypatch.setattr(tx_app, "TransmitWindow", window)
    monkeypatch.setattr(tx_app.qt_runtime, "create_application", lambda: None)
    overlay = _overlay(tmp_path, cpu_cores=4)
    with pytest.raises(_Reached):
        tx_app.main(["--config", DEFAULT, "--overlay", ONE_WAY, "--overlay", overlay])
    assert recorded_budget == [4]
    assert cpu_budget.requested() == 4


def test_the_engine_process_applies_the_recorded_budget(monkeypatch, recorded_budget):
    from ofdm_link.runtime import allocator
    from ofdm_message_link import engine

    monkeypatch.setattr(allocator, "keep_freed_memory", lambda: None)
    engine._bound_numeric_threads()
    monkeypatch.setenv(cpu_budget.ENVIRONMENT, "6")
    engine._bound_numeric_threads()
    assert recorded_budget == [1, 6]


def test_the_resolved_count_sizes_the_decode_pool(tmp_path):
    from ofdm_message_link.link import MessageReceiver

    overlay = _overlay(tmp_path, decode_workers=2)
    args = rx_app.build_parser().parse_args(
        ["--config", DEFAULT, "--overlay", ONE_WAY, "--overlay", overlay]
    )
    resolved = options.resolve(args)
    workers = options.resolve_decode_workers(args, resolved.config, rx_app.DEFAULT_DECODE_WORKERS)
    receiver = MessageReceiver(resolved.profile, decode_workers=workers)
    try:
        assert receiver.decode_workers == 2
    finally:
        receiver.close()

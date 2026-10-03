from __future__ import annotations

import os
import subprocess
import sys

import ofdm_link.phy as legacy_phy
import ofdm_link.phy.facade as facade

SUPPORTED_PHY_API = frozenset(
    {
        "BurstConfig",
        "BurstDecodeError",
        "CURRENT_PROTOCOL_VERSION",
        "DecodedBurst",
        "Frame",
        "FrameKind",
        "MCS",
        "StreamingBurstDecoder",
        "deserialize_frame",
        "encode_burst",
        "serialize_frame",
    }
)


def test_supported_facade_is_small_and_legacy_direct_imports_remain_available() -> None:
    assert frozenset(facade.__all__) == SUPPORTED_PHY_API
    assert 0 < len(facade.__all__) < len(legacy_phy.__all__)
    assert all(hasattr(facade, name) for name in facade.__all__)
    assert not hasattr(legacy_phy, "StreamingBurstDecoder")
    assert not hasattr(legacy_phy, "serialize_frame")

    # Representative low-level compatibility exports stay importable, but are
    # deliberately outside the supported application-facing facade.
    assert legacy_phy.NativeTurboFecAdapter is not None
    assert legacy_phy.pilot_symbols is not None
    assert "NativeTurboFecAdapter" not in facade.__all__
    assert "pilot_symbols" not in facade.__all__


def test_supported_facade_import_is_headless() -> None:
    environment = os.environ.copy()
    environment.pop("DISPLAY", None)
    command = (
        "import sys; import ofdm_link.phy.facade; "
        "assert not any(name.startswith(('PyQt', 'PySide')) for name in sys.modules)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr

"""--cpu-affinity: off by default, explicit lists only, p-cores only where sysfs has them."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ofdm_link.runtime import cpu_affinity
from ofdm_message_link import node_options, options, rx_app, tx_app


def test_cpu_lists_parse_like_taskset():
    assert cpu_affinity.parse_cpu_list("0-3,8,10-11") == {0, 1, 2, 3, 8, 10, 11}
    assert cpu_affinity.format_cpu_list({0, 1, 2, 5, 7, 8}) == "0-2,5,7-8"
    for bad in ("", "a", "3-1", "1,,2", "-1"):
        with pytest.raises(cpu_affinity.CpuAffinityError):
            cpu_affinity.parse_cpu_list(bad)


def test_default_is_no_pinning_in_every_app():
    parsers = (rx_app.build_parser(), tx_app.build_parser(), node_options.build_parser())
    for parser in parsers:
        args = parser.parse_args(["--node-id", "1"] if parser.prog == "ofdm-message-node" else [])
        assert args.cpu_affinity is None
    before = os.sched_getaffinity(0)
    assert cpu_affinity.apply(None) is None
    assert os.sched_getaffinity(0) == before


def test_bad_list_is_rejected_at_the_command_line():
    with pytest.raises(SystemExit):
        rx_app.build_parser().parse_args(["--cpu-affinity", "7-3"])


def test_p_cores_without_sysfs_pins_nothing(tmp_path):
    lines = []
    assert cpu_affinity.apply("p-cores", core_list=tmp_path / "missing", log=lines.append) is None
    assert len(lines) == 1 and "does not exist" in lines[0]


def test_p_cores_reads_the_sysfs_list(tmp_path):
    listing = tmp_path / "cpus"
    listing.write_text("0-3\n")
    assert cpu_affinity.resolve("p-cores", core_list=listing) == {0, 1, 2, 3}


def test_pinning_is_inherited_by_child_processes():
    cpu = min(os.sched_getaffinity(0))
    code = (
        "import os, subprocess, sys\n"
        "from ofdm_link.runtime import cpu_affinity\n"
        f"cpu_affinity.apply('{cpu}', log=lambda line: None)\n"
        "out = subprocess.run([sys.executable, '-c', "
        "'import os; print(sorted(os.sched_getaffinity(0)))'], capture_output=True, text=True)\n"
        "print(out.stdout.strip())\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == f"[{cpu}]"


def test_a_cpu_outside_the_allowed_set_is_an_error():
    outside = max(os.sched_getaffinity(0)) + 1000
    with pytest.raises(cpu_affinity.CpuAffinityError):
        cpu_affinity.apply(str(outside), log=lambda line: None)


def test_apply_performance_options_exits_cleanly_on_a_bad_cpu():
    outside = max(os.sched_getaffinity(0)) + 1000
    args = rx_app.build_parser().parse_args(["--cpu-affinity", str(outside)])
    with pytest.raises(SystemExit, match="--cpu-affinity"):
        options.apply_performance_options(args)

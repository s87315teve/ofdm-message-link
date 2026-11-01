"""What the duplex hardware scripts share: the run gates, the meta record, the recorder.

Every hardware run here follows the same rules:

* one heavy job at a time: ``flock /tmp/ofdm_hw.lock``;
* start only while the 1-minute load average is below ``--max-load``
  (wait up to 10 min, then abort), log it every second, and mark the run
  invalid if it goes above 8;
* record ``lsusb -t``, the git revision and the load in the output;
* a Pluto that does not open or hangs gets at most 3 retries, then the run
  stops and says so.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np

HW_LOCK_PATH = "/tmp/ofdm_hw.lock"
INVALID_LOAD = 8.0
NODE_A_SERIAL = "104473541196000419000c00f2ff1e6e77"
NODE_B_SERIAL = "10447354119600041b000b00f2ff1ef9c6"
RX_FULL_SCALE = 2048.0
OPEN_RETRIES = 3
SAMPLE_RATE = 5_000_000


def loadavg() -> list[float]:
    with open("/proc/loadavg", encoding="ascii") as handle:
        return [float(value) for value in handle.read().split()[:3]]


def wait_for_load(max_load: float, *, wait_s: float = 600.0, sleep=time.sleep) -> list[float]:
    """Block until load1 < ``max_load``; SystemExit after ``wait_s``."""

    deadline = time.monotonic() + wait_s
    while True:
        load = loadavg()
        if load[0] < max_load:
            return load
        if time.monotonic() >= deadline:
            raise SystemExit(f"1-min load {load[0]} stayed >= {max_load}; not starting")
        print(f"load {load[0]} >= {max_load}, waiting", file=sys.stderr, flush=True)
        sleep(10.0)


@contextlib.contextmanager
def hardware_lock(path: str = HW_LOCK_PATH, *, wait_s: float = 600.0) -> Iterator[None]:
    """Hold the one-heavy-job lock for the duration of the block."""

    with open(path, "a+", encoding="ascii") as handle:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SystemExit(f"{path} is held by another hardware job") from None
                time.sleep(2.0)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class LoadLogger:
    """Append the load average to a file once a second; remember the maximum."""

    def __init__(self, path: Path, period_s: float = 1.0) -> None:
        self._path = path
        self._period_s = period_s
        self._stop = threading.Event()
        self.max_load1 = 0.0
        self._thread = threading.Thread(target=self._run, name="loadavg", daemon=True)

    def start(self) -> LoadLogger:
        self._thread.start()
        return self

    def _run(self) -> None:
        with open(self._path, "a", encoding="ascii") as handle:
            while not self._stop.is_set():
                load = loadavg()
                self.max_load1 = max(self.max_load1, load[0])
                handle.write(f"{time.time():.3f} {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}\n")
                handle.flush()
                self._stop.wait(self._period_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    @property
    def invalid(self) -> bool:
        return self.max_load1 > INVALID_LOAD


def lsusb_tree() -> str:
    try:
        return subprocess.run(
            ["lsusb", "-t"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return f"lsusb failed: {error}"


def git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def base_meta(args: Any) -> dict[str, object]:
    return {
        "argv": sys.argv,
        "args": {key: value for key, value in vars(args).items() if key != "acknowledgement"},
        "git_revision": git_revision(),
        "lsusb_t": lsusb_tree(),
        "loadavg_at_start": loadavg(),
        "cpu_count": os.cpu_count(),
        "thread_env": {
            name: os.environ.get(name)
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
        "started_unix": time.time(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=_json_default) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _json_default(value: object) -> object:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return str(value)


def with_retries(
    what: str,
    attempt: Callable[[], Any],
    *,
    retries: int = OPEN_RETRIES,
    pause_s: float = 8.0,
    sleep=time.sleep,
) -> tuple[Any, int]:
    """Run ``attempt`` with up to ``retries`` retries; SystemExit when all fail."""

    last: BaseException | None = None
    for index in range(retries + 1):
        try:
            return attempt(), index
        except Exception as error:  # noqa: BLE001 - Pluto hangs surface as many types
            last = error
            print(f"{what} failed (attempt {index}): {error}", file=sys.stderr, flush=True)
            if index < retries:
                sleep(pause_s)
    raise SystemExit(f"{what} failed after {retries} retries: {last}")


def find_plutos(serials: tuple[str, ...]) -> dict[str, Any]:
    """Discover the named Plutos; raise when one is missing."""

    from ofdm_message_link import pluto

    devices = {device.serial: device for device in pluto.discover()}
    missing = [serial for serial in serials if serial not in devices]
    if missing:
        raise RuntimeError(f"Pluto(s) {missing} not found (have {sorted(devices)})")
    return devices


class Recorder(threading.Thread):
    """Drain a running sample source; keep the chunks while armed, count all of them."""

    def __init__(self, source: Any) -> None:
        super().__init__(daemon=True, name="recorder")
        self.source = source
        self.stop_event = threading.Event()
        self.armed = threading.Event()
        self.samples_seen = 0
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()

    def run(self) -> None:
        while not self.stop_event.is_set():
            chunk = self.source.recv(0.1)
            if chunk is None:
                continue
            self.samples_seen += int(chunk.size)
            if self.armed.is_set():
                with self._lock:
                    self._chunks.append(chunk)

    def take(self) -> np.ndarray:
        """The armed samples as complex64 at RX full scale 1.0, and forget them."""

        with self._lock:
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.complex64)
        return np.concatenate(chunks)

    def close(self) -> None:
        self.stop_event.set()
        self.join(timeout=2.0)


def to_int16(samples: np.ndarray) -> np.ndarray:
    """Back to the interleaved int16 the ADC delivered (12 bit, full scale 2048)."""

    return np.rint(samples.view(np.float32) * RX_FULL_SCALE).astype(np.int16)


def acknowledge(args: Any):
    """The RF token; exits unless --enable-rf and the exact acknowledgement were given."""

    from ofdm_message_link.options import require_rf_capability

    args.transport = "pluto"
    return require_rf_capability(args)


def add_rf_arguments(parser) -> None:
    parser.add_argument("--enable-rf", action="store_true")
    parser.add_argument("--acknowledgement", default=None)
    parser.add_argument(
        "--max-load",
        type=float,
        default=4.0,
        help="start only below this 1-min load average (default 4)",
    )


class Pacer(threading.Thread):
    """Push pre-encoded bursts to a sink at a duty cycle or a burst rate."""

    def __init__(
        self,
        sink,
        bursts,
        *,
        duty: float | None = None,
        rate: float | None = None,
        count: int | None = None,
        sample_rate: float = SAMPLE_RATE,
    ) -> None:
        super().__init__(daemon=True, name="pacer")
        self.sample_rate = float(sample_rate)
        self.sink = sink
        self.bursts = bursts
        self.duty = duty
        self.rate = rate
        self.count = count
        self.stop_event = threading.Event()
        self.sent = 0
        self.samples = 0
        self.sent_indices: list[int] = []
        self.elapsed_s = 0.0
        self.max_send_s = 0.0
        self.error: str | None = None

    def run(self) -> None:
        started = time.monotonic()
        due = started
        index = 0
        try:
            while not self.stop_event.is_set():
                if self.count is not None and index >= self.count:
                    break
                samples = self.bursts[index % len(self.bursts)]
                delay = due - time.monotonic()
                if delay > 0:
                    self.stop_event.wait(delay)
                    if self.stop_event.is_set():
                        break
                pushed = time.monotonic()
                self.sink.send(samples)
                self.max_send_s = max(self.max_send_s, time.monotonic() - pushed)
                self.sent_indices.append(index % len(self.bursts))
                self.sent += 1
                self.samples += int(samples.size)
                if self.duty:
                    due += samples.size / self.sample_rate / self.duty
                else:
                    due += 1.0 / float(self.rate)
                index += 1
        except Exception as error:  # noqa: BLE001 - reported in the record
            self.error = f"{type(error).__name__}: {error}"
        self.elapsed_s = time.monotonic() - started

    def finish(self) -> None:
        self.stop_event.set()
        self.join(timeout=5.0)

    @property
    def duty_achieved(self) -> float | None:
        if self.elapsed_s <= 0:
            return None
        return round(self.samples / self.sample_rate / self.elapsed_s, 3)

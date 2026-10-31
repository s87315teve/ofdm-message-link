#!/usr/bin/env bash
# One live Pluto-to-Pluto run of the real apps (rx_app, tx_app, paced UDP load),
# both apps and their configs taken from ROOT (any checkout/worktree).
#
#   experiments/tx_v5/live_run.sh ROOT TX_GAIN MCS_INDEX RATE SECONDS OUT [SIZE]
#
# Writes OUT/{rx_stats.jsonl,load.json,meta.json,...}; meta.json has the 1-min
# load at start and decode-worker CPU during the load.  Transmits RF.
set -euo pipefail

ROOT=$(realpath "$1"); TX_GAIN=$2; MCS=$3; RATE=$4; SECONDS_=$5; OUT=$6; SIZE=${7:-900}
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python}
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
mkdir -p "$OUT"
LOAD_START=$(cut -d' ' -f1 /proc/loadavg)
mapfile -t SERIALS < <(cd "$ROOT" && PYTHONPATH="$ROOT/src" "$PYTHON" -c \
    'from ofdm_message_link import pluto; [print(d.serial) for d in pluto.discover()]')
[ "${#SERIALS[@]}" -eq 2 ] || { echo "need exactly two Plutos" >&2; exit 1; }
TX_SERIAL=${SERIALS[0]}; RX_SERIAL=${SERIALS[1]}
export QT_QPA_PLATFORM=offscreen
rm -f "$OUT/rx.ready" "$OUT/tx.ready" "$OUT/cpu.jsonl"

cleanup() { kill "${SAMPLER_PID:-}" "${TX_PID:-}" "${RX_PID:-}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT

wait_ready() {
    for _ in $(seq 120); do [ -f "$1" ] && return 0; sleep 0.5; done
    echo "radio did not start: $1" >&2; exit 1
}

cd "$ROOT"
PYTHONPATH="$ROOT/src" "$PYTHON" -m ofdm_message_link.rx_app --transport pluto \
    --config "$ROOT/configs/default.yaml" \
    --overlay "$ROOT/configs/profiles/ota_2p45ghz.yaml" \
    --serial "$RX_SERIAL" --gain 15 --auto-start --ready-file "$OUT/rx.ready" \
    --egress-port 52022 --stats-log "$OUT/rx_stats.jsonl" > "$OUT/rx.log" 2>&1 &
RX_PID=$!
wait_ready "$OUT/rx.ready"
PYTHONPATH="$ROOT/src" "$PYTHON" -m ofdm_message_link.tx_app --transport pluto \
    --serial "$TX_SERIAL" --gain "$TX_GAIN" --mcs-index "$MCS" --auto-start \
    --ready-file "$OUT/tx.ready" \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF' \
    > "$OUT/tx.log" 2>&1 &
TX_PID=$!
wait_ready "$OUT/tx.ready"
sleep 1
LOAD_START=$(cut -d' ' -f1 /proc/loadavg)
"$PYTHON" "$HERE/cpu_sampler.py" "$RX_PID" "$OUT/cpu.jsonl" 1.0 &
SAMPLER_PID=$!
"$PYTHON" "$HERE/../ota_udp_throughput/udp_load.py" --rate "$RATE" \
    --duration "$SECONDS_" --size "$SIZE" --output "$OUT/load.json" > "$OUT/load.log" 2>&1
kill "$SAMPLER_PID" 2>/dev/null || true; wait "$SAMPLER_PID" 2>/dev/null || true
"$PYTHON" - "$OUT" "$RX_PID" "$LOAD_START" "$ROOT" "$TX_GAIN" "$MCS" "$RATE" "$SIZE" "$HERE" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[9])
import cpu_sampler
out, pid, load, root, gain, mcs, rate, size = sys.argv[1:9]
meta = {"root": root, "tx_gain_db": float(gain), "mcs_index": int(mcs), "rate": float(rate),
        "size": int(size), "load_1min_at_start": float(load)}
meta.update(cpu_sampler.summarize(out + "/cpu.jsonl", int(pid)))
json.dump(meta, open(out + "/meta.json", "w"), indent=1)
PY

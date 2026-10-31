#!/usr/bin/env bash
# One live Pluto-to-Pluto run of the real apps: rx_app, tx_app, paced UDP load.
#
#   experiments/rx_improvement/live_run.sh RX_SRC TX_GAIN MCS_INDEX RATE SECONDS OUT
#
# RX_SRC is the src/ directory rx_app is imported from, so the same command
# runs the v1.0.0 receiver (a checkout's src/) or this branch's (src).  The
# receiver also reads the configs/ beside RX_SRC, so each one acquires with its
# own thresholds.  The transmitter always comes from this tree; its code is
# unchanged from v1.0.0.
# This transmits RF.  The two Plutos are picked by serial, lowest serial TX.
set -euo pipefail

RX_SRC=$(realpath "$1"); RX_ROOT=$(dirname "$RX_SRC"); TX_GAIN=$2; MCS=$3; RATE=$4; SECONDS_=$5; OUT=$6
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=${PYTHON:-python}
mkdir -p "$OUT"
mapfile -t SERIALS < <(PYTHONPATH="$ROOT/src" "$PYTHON" -c \
    'from ofdm_message_link import pluto; [print(d.serial) for d in pluto.discover()]')
[ "${#SERIALS[@]}" -eq 2 ] || { echo "need exactly two Plutos" >&2; exit 1; }
TX_SERIAL=${SERIALS[0]}; RX_SERIAL=${SERIALS[1]}
export QT_QPA_PLATFORM=offscreen
rm -f "$OUT/rx.ready" "$OUT/tx.ready"

cleanup() { kill "${TX_PID:-}" "${RX_PID:-}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT

wait_ready() {
    for _ in $(seq 120); do [ -f "$1" ] && return 0; sleep 0.5; done
    echo "radio did not start: $1" >&2; exit 1
}

cd "$ROOT"
PYTHONPATH="$RX_SRC" "$PYTHON" -m ofdm_message_link.rx_app --transport pluto \
    --config "$RX_ROOT/configs/default.yaml" \
    --overlay "$RX_ROOT/configs/profiles/ota_2p45ghz.yaml" \
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
"$PYTHON" experiments/ota_udp_throughput/udp_load.py --rate "$RATE" \
    --duration "$SECONDS_" --size 900 --output "$OUT/load.json" > "$OUT/load.log" 2>&1

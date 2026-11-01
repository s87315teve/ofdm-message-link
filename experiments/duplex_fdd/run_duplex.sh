#!/usr/bin/env bash
# One bidirectional FDD run of the real node_app on two Plutos, with one paced,
# byte-checked UDP load per direction (A = node 1, B = node 2).
#
#   A -> B: udp_load -> node1 ingress -> RF -> node2 egress -> udp_load check
#   B -> A: udp_load -> node2 ingress -> RF -> node1 egress -> udp_load check
#
# Transmits RF.  Needs --enable-rf.  Takes /tmp/ofdm_hw.lock, waits for the 1-min
# load to fall below 4, and kills only the PIDs it started.  See --help.
set -euo pipefail

# ---- node_app CLI as designed (design_duplex.md section 2.5); adjust at integration ----
NODE_MODULE=${NODE_MODULE:-ofdm_message_link.node_app}
F_NODE_ID=--node-id            F_OVERLAY=--overlay          F_TRANSPORT=--transport
F_SERIAL=--serial              F_HEADLESS=--headless        F_DURATION=--duration
F_READY=--ready-file           F_STATS=--stats-log          F_LA_LOG=--la-log
F_LA=--link-adaptation         F_LA_ARG=--link-adaptation-arg
F_MCS=--mcs-index              F_WORKERS=--decode-workers   F_TX_GAIN=--tx-gain
F_RX_GAIN=--rx-gain            F_GAIN_PROFILE=--gain-profile
F_ENABLE_RF=--enable-rf        F_ACK=--acknowledgement
ACK_TEXT='I acknowledge that this process will transmit RF'
INGRESS_PORT=(0 52211 52221)   # index = node id
EGRESS_PORT=(0 52212 52222)
SERIAL=(0 104473541196000419000c00f2ff1e6e77 10447354119600041b000b00f2ff1ef9c6)
UDP_LOAD=experiments/ota_udp_throughput/udp_load.py
# -------------------------------------------------------------------------------------

usage() {
    cat <<'EOF2'
usage: experiments/duplex_fdd/run_duplex.sh --enable-rf --out DIR [options]

  --out DIR              run directory (created)
  --enable-rf            required: both nodes key their transmitters
  --dry-run              print the commands and exit (no lock, gate or hardware)
  --overlay PATH         profile overlay (default configs/profiles/duplex_fdd_2420_2460.yaml)
  --serial1 S --serial2 S   Pluto serials (defaults: the two on this bench)
  --rate-ab R  --rate-ba R  datagrams/s per direction (default 450 / 450; 0 = direction idle)
  --size N               datagram bytes (default 900)
  --duration S           load duration (default 60)
  --mcs N | --mcs1 N --mcs2 N            fixed/initial MCS (default 8)
  --la SPEC | --la1 SPEC --la2 SPEC      link adaptation (default fixed)
  --la-arg k=v           LA argument for both nodes, repeatable (--la1-arg / --la2-arg per node)
  --tx-gain DB | --tx-gain1 DB --tx-gain2 DB   initial TX gain (default -20)
  --rx-gain DB           RX gain both nodes (default 15)
  --gain-profile P | --gain-profile1 P --gain-profile2 P   e.g. "0:-5,30:-20,60:-5" (s from ready)
  --decode-workers N     per node (default 3)
  --gate-load X          1-min loadavg must be below X before start (default 4)
  --gate-wait S          wait this long for the gate, then exit 3 (default 600)
  --settle S             seconds after both nodes are ready before the load (default 3)
  --tail S               seconds the nodes keep running after the load (default 4)
  --extra-node-arg A     passed to both node_app (repeatable)
  --ready-timeout S      per node (default 90)
  --hook CMD             run CMD (bash -c, env OUT=run dir) instead of the udp_load pair once both
                         nodes are ready; the run ends when it exits (used by video_duplex.sh)
EOF2
}

ENABLE_RF=0 DRY=0 OUT=""
OVERLAY=configs/profiles/duplex_fdd_2420_2460.yaml
RATE_AB=450 RATE_BA=450 SIZE=900 DURATION=60
MCS=(0 8 8) LA=(0 fixed fixed) TXG=(0 -20 -20) PROFILE=(0 "" "")
RX_GAIN=15 WORKERS=3 GATE_LOAD=4 GATE_WAIT=600 SETTLE=3 TAIL=4 READY_TIMEOUT=90
LA_ARGS=(0 "" "") EXTRA=() HOOK=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift ;;
        --enable-rf) ENABLE_RF=1 ;;
        --dry-run) DRY=1 ;;
        --overlay) OVERLAY="$2"; shift ;;
        --serial1) SERIAL[1]="$2"; shift ;;
        --serial2) SERIAL[2]="$2"; shift ;;
        --rate-ab) RATE_AB="$2"; shift ;;
        --rate-ba) RATE_BA="$2"; shift ;;
        --size) SIZE="$2"; shift ;;
        --duration) DURATION="$2"; shift ;;
        --mcs) MCS[1]="$2"; MCS[2]="$2"; shift ;;
        --mcs1) MCS[1]="$2"; shift ;;
        --mcs2) MCS[2]="$2"; shift ;;
        --la) LA[1]="$2"; LA[2]="$2"; shift ;;
        --la1) LA[1]="$2"; shift ;;
        --la2) LA[2]="$2"; shift ;;
        --la-arg) LA_ARGS[1]+="$2"$'\n'; LA_ARGS[2]+="$2"$'\n'; shift ;;
        --la1-arg) LA_ARGS[1]+="$2"$'\n'; shift ;;
        --la2-arg) LA_ARGS[2]+="$2"$'\n'; shift ;;
        --tx-gain) TXG[1]="$2"; TXG[2]="$2"; shift ;;
        --tx-gain1) TXG[1]="$2"; shift ;;
        --tx-gain2) TXG[2]="$2"; shift ;;
        --rx-gain) RX_GAIN="$2"; shift ;;
        --gain-profile) PROFILE[1]="$2"; PROFILE[2]="$2"; shift ;;
        --gain-profile1) PROFILE[1]="$2"; shift ;;
        --gain-profile2) PROFILE[2]="$2"; shift ;;
        --decode-workers) WORKERS="$2"; shift ;;
        --gate-load) GATE_LOAD="$2"; shift ;;
        --gate-wait) GATE_WAIT="$2"; shift ;;
        --settle) SETTLE="$2"; shift ;;
        --tail) TAIL="$2"; shift ;;
        --extra-node-arg) EXTRA+=("$2"); shift ;;
        --ready-timeout) READY_TIMEOUT="$2"; shift ;;
        --hook) HOOK="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[ -n "$OUT" ] || { echo "--out is required" >&2; exit 2; }
if [ "$ENABLE_RF" -ne 1 ] && [ "$DRY" -ne 1 ]; then
    echo "refusing to transmit: pass --enable-rf" >&2; exit 2
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
HERE="$REPO/experiments/duplex_fdd"
PYTHON=${PYTHON:-/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python}
[ -x "$PYTHON" ] || PYTHON=python
export PYTHONPATH="$REPO/src" OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export QT_QPA_PLATFORM=offscreen SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy
NODE_DURATION=$(( ${DURATION%.*} + 120 ))   # backstop only; the script stops the nodes

node_cmd() {  # node id -> prints the command, one word per line
    local n=$1 line
    printf '%s\n' "$PYTHON" -m "$NODE_MODULE" "$F_NODE_ID" "$n" "$F_OVERLAY" "$OVERLAY" \
        "$F_TRANSPORT" pluto "$F_SERIAL" "${SERIAL[$n]}" "$F_HEADLESS" \
        "$F_READY" "$OUT/node$n.ready" "$F_STATS" "$OUT/node${n}_stats.jsonl" \
        "$F_LA_LOG" "$OUT/la_$n.jsonl" "$F_LA" "${LA[$n]}" "$F_MCS" "${MCS[$n]}" \
        "$F_WORKERS" "$WORKERS" "$F_TX_GAIN" "${TXG[$n]}" "$F_RX_GAIN" "$RX_GAIN" \
        "$F_DURATION" "$NODE_DURATION" "$F_ENABLE_RF" "$F_ACK" "$ACK_TEXT"
    if [ -n "${PROFILE[$n]}" ]; then printf '%s\n' "$F_GAIN_PROFILE" "${PROFILE[$n]}"; fi
    while IFS= read -r line; do
        if [ -n "$line" ]; then printf '%s\n' "$F_LA_ARG" "$line"; fi
    done <<< "${LA_ARGS[$n]}"
    if [ "${#EXTRA[@]}" -gt 0 ]; then printf '%s\n' "${EXTRA[@]}"; fi
    return 0
}
load_cmd() {  # dir rate ingress egress
    printf '%s\n' "$PYTHON" "$REPO/$UDP_LOAD" --rate "$2" --duration "$DURATION" --size "$SIZE" \
        --ingress-port "$3" --egress-port "$4" --output "$OUT/load_$1.json"
}

if [ "$DRY" -eq 1 ]; then
    echo "# out: $OUT   (flock /tmp/ofdm_hw.lock, wait for 1-min load < $GATE_LOAD)"
    for n in 1 2; do
        mapfile -t c < <(node_cmd "$n"); echo "node$n: ${c[*]}"
    done
    if [ "$RATE_AB" != 0 ]; then
        mapfile -t c < <(load_cmd AB "$RATE_AB" "${INGRESS_PORT[1]}" "${EGRESS_PORT[2]}"); echo "load AB: ${c[*]}"
    fi
    if [ "$RATE_BA" != 0 ]; then
        mapfile -t c < <(load_cmd BA "$RATE_BA" "${INGRESS_PORT[2]}" "${EGRESS_PORT[1]}"); echo "load BA: ${c[*]}"
    fi
    echo "cpu: $PYTHON $HERE/cpu_multi.py $OUT/cpu.jsonl 1.0 1=<pid1> 2=<pid2>"
    exit 0
fi

mkdir -p "$OUT"
exec 9>/tmp/ofdm_hw.lock
echo "waiting for /tmp/ofdm_hw.lock ..."
flock 9
waited=0
while :; do
    load1=$(cut -d' ' -f1 /proc/loadavg)
    if awk -v l="$load1" -v g="$GATE_LOAD" 'BEGIN{exit !(l<g)}'; then break; fi
    if [ "$waited" -ge "$GATE_WAIT" ]; then echo "load $load1 >= $GATE_LOAD after ${GATE_WAIT}s" >&2; exit 3; fi
    sleep 5; waited=$(( waited + 5 ))
done
LOAD_START=$(cat /proc/loadavg)

NODE_PID=(0 "" "") LOAD_PIDS=() SAMPLER_PID="" LOADAVG_PID=""
stop_pid() {  # pid seconds
    if [ -z "$1" ] || ! kill -0 "$1" 2>/dev/null; then return 0; fi
    kill -TERM "$1" 2>/dev/null || true
    for _ in $(seq $(( $2 * 10 ))); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done
    kill -KILL "$1" 2>/dev/null || true
}
cleanup() {  # kills exactly the PIDs this script started
    trap - EXIT INT TERM
    local p
    for p in "${LOAD_PIDS[@]:-}" "$SAMPLER_PID" "$LOADAVG_PID"; do stop_pid "$p" 2; done
    for p in "${NODE_PID[1]}" "${NODE_PID[2]}"; do stop_pid "$p" 10; done
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

rm -f "$OUT"/node?.ready "$OUT"/cpu.jsonl
( while :; do echo "$(date +%s.%N) $(cat /proc/loadavg)"; sleep 1; done > "$OUT/loadavg.log" ) &
LOADAVG_PID=$!

wait_ready() {  # node
    local n=$1 w=0
    until [ -f "$OUT/node$n.ready" ]; do
        kill -0 "${NODE_PID[$n]}" 2>/dev/null || { echo "node$n exited before ready; see $OUT/node$n.log" >&2; exit 1; }
        if [ "$w" -ge $(( READY_TIMEOUT * 2 )) ]; then echo "node$n not ready in ${READY_TIMEOUT}s" >&2; exit 1; fi
        sleep 0.5; w=$(( w + 1 ))
    done
}
# Node 2 first, so it listens before node 1 keys up.
for n in 2 1; do
    mapfile -t c < <(node_cmd "$n")
    "${c[@]}" > "$OUT/node$n.log" 2>&1 < /dev/null &
    NODE_PID[n]=$!
    wait_ready "$n"
done
sleep "$SETTLE"

"$PYTHON" "$HERE/cpu_multi.py" "$OUT/cpu.jsonl" 1.0 "1=${NODE_PID[1]}" "2=${NODE_PID[2]}" &
SAMPLER_PID=$!

# meta.json before the load, so a killed run still has its description.
OUT="$OUT" REPO="$REPO" LOAD_START="$LOAD_START" PID1="${NODE_PID[1]}" PID2="${NODE_PID[2]}" \
SER1="${SERIAL[1]}" SER2="${SERIAL[2]}" OVERLAY="$OVERLAY" RATE_AB="$RATE_AB" RATE_BA="$RATE_BA" \
SIZE="$SIZE" DURATION="$DURATION" MCS1="${MCS[1]}" MCS2="${MCS[2]}" LA1="${LA[1]}" LA2="${LA[2]}" \
LAARGS1="${LA_ARGS[1]}" LAARGS2="${LA_ARGS[2]}" TXG1="${TXG[1]}" TXG2="${TXG[2]}" RX_GAIN="$RX_GAIN" \
PROF1="${PROFILE[1]}" PROF2="${PROFILE[2]}" WORKERS="$WORKERS" ARGV="$*" \
"$PYTHON" "$HERE/write_meta.py"

LOAD_PIDS=()
if [ -n "$HOOK" ]; then
    OUT="$OUT" bash -c "$HOOK" > "$OUT/hook.log" 2>&1 &
    LOAD_PIDS+=("$!")
    RATE_AB=0 RATE_BA=0
fi
for spec in "AB $RATE_AB 1 2" "BA $RATE_BA 2 1"; do
    read -r d rate src dst <<< "$spec"
    if [ "$rate" = 0 ]; then continue; fi
    mapfile -t c < <(load_cmd "$d" "$rate" "${INGRESS_PORT[$src]}" "${EGRESS_PORT[$dst]}")
    "${c[@]}" > "$OUT/load_$d.log" 2>&1 &
    LOAD_PIDS+=("$!")
done
for p in "${LOAD_PIDS[@]}"; do wait "$p" || echo "a load exited non-zero (see load_*.log)" >&2; done
sleep "$TAIL"
echo "run complete: $OUT"

#!/usr/bin/env bash
# M4: video in both directions at once over the duplex link, link adaptation on.
#
#   A -> B: webcam   -> ffmpeg H.264 -> node1 ingress 52211 ... node2 egress 52222 -> ffmpeg -f null
#   B -> A: testsrc2 -> ffmpeg H.264 -> node2 ingress 52221 ... node1 egress 52212 -> ffmpeg -f null
#
# The nodes are started by run_duplex.sh (--hook), so the run directory has the same
# files as every other run, plus video_{AB,BA}_{tx,rx}.log, video_*_progress.txt and
# video_counts.json (frames, corrupt and error lines per direction).  Headless-safe.
# The ffmpeg encoder settings are those of scripts/run_ota_video_demo.sh.
set -euo pipefail

# ---- ffmpeg settings (same as the one-way demo) and ports; edit here ----
CAMERA=${CAMERA:-/dev/video0}
CAM_INPUT=(-f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15)
LAVFI_SRC=${LAVFI_SRC:-testsrc2=size=1280x720:rate=15}
ENC_EXTRA=(-vf format=yuv420p -c:v libx264 -preset ultrafast -tune zerolatency)
X264_PARAMS="nal-hrd=cbr:force-cfr=1:intra-refresh=1:keyint=30:repeat-headers=1"
PKT_SIZE=752
INGRESS_A=52211 EGRESS_A=52212 INGRESS_B=52221 EGRESS_B=52222   # node 1 = A, node 2 = B
# -------------------------------------------------------------------------

usage() {
    cat <<'EOF'
usage: experiments/duplex_fdd/video_duplex.sh --enable-rf --out DIR [options] [-- run_duplex args]

  --out DIR          run directory
  --enable-rf        required (passed to run_duplex.sh)
  --dry-run          print what would run and exit
  --kbps N           H.264 bit rate per direction (default 750; 1300 if M3 shows MCS >= 10 at -5 dB)
  --seconds N        video duration (default 60)
  --camera DEV       A -> B source (default /dev/video0)
  --la SPEC          link adaptation on both nodes (default olla)
  --mcs N            initial MCS (default 8)
  --tx-gain DB       TX gain both nodes (default -5)
  everything after -- goes to run_duplex.sh (e.g. --decode-workers 2, --gain-profile1 ...).
  Internal: --inner runs the producers and consumers (called by run_duplex.sh --hook).
EOF
}

export QT_QPA_PLATFORM=offscreen SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy
KBPS=750 SECONDS_=60 LA=olla MCS=8 TXG=-5 OUT="" RF=() DRY=0 INNER=0 PASS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift ;;
        --enable-rf) RF=(--enable-rf) ;;
        --dry-run) DRY=1 ;;
        --kbps) KBPS="$2"; shift ;;
        --seconds) SECONDS_="$2"; shift ;;
        --camera) CAMERA="$2"; shift ;;
        --la) LA="$2"; shift ;;
        --mcs) MCS="$2"; shift ;;
        --tx-gain) TXG="$2"; shift ;;
        --inner) INNER=1 ;;
        --) shift; PASS=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[ -n "$OUT" ] || { echo "--out is required" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
MUX_KBPS=$(( KBPS * 111 / 100 ))   # video rate plus TS/PES overhead, as in the demo

producer_cmd() {  # label ingress-port input-args...
    local port=$2; shift 2
    printf '%s\n' ffmpeg -nostdin -nostats -loglevel warning "$@" "${ENC_EXTRA[@]}" \
        -b:v "${KBPS}k" -maxrate "${KBPS}k" -minrate "${KBPS}k" -bufsize "$(( KBPS / 2 ))k" \
        -x264-params "$X264_PARAMS" -muxrate "${MUX_KBPS}k" -t "$SECONDS_" \
        -f mpegts "udp://127.0.0.1:${port}?pkt_size=${PKT_SIZE}"
}
consumer_cmd() {  # label egress-port; default error handling, we want the counts
    printf '%s\n' ffmpeg -nostdin -nostats -loglevel level+warning -progress "$OUT/video_$1_progress.txt" \
        -i "udp://127.0.0.1:$2?fifo_size=100000&overrun_nonfatal=1&timeout=5000000" -f null -
}

if [ "$INNER" -ne 1 ]; then
    outer=("$HERE/run_duplex.sh" --out "$OUT" "${RF[@]}" --la "$LA" --mcs "$MCS" --tx-gain "$TXG"
           --duration "$SECONDS_" --hook "bash $HERE/video_duplex.sh --inner --out $OUT --kbps $KBPS --seconds $SECONDS_ --camera $CAMERA"
           "${PASS[@]}")
    if [ "$DRY" -eq 1 ]; then
        echo "outer: ${outer[*]}"
        mapfile -t c < <(producer_cmd AB "$INGRESS_A" "${CAM_INPUT[@]}" -i "$CAMERA"); echo "A->B producer: ${c[*]}"
        mapfile -t c < <(producer_cmd BA "$INGRESS_B" -re -f lavfi -i "$LAVFI_SRC"); echo "B->A producer: ${c[*]}"
        mapfile -t c < <(consumer_cmd AB "$EGRESS_B"); echo "A->B consumer: ${c[*]}"
        mapfile -t c < <(consumer_cmd BA "$EGRESS_A"); echo "B->A consumer: ${c[*]}"
        exit 0
    fi
    if [ "${#RF[@]}" -eq 0 ]; then echo "pass --enable-rf (or --dry-run)" >&2; exit 2; fi
    exec "${outer[@]}"
fi

# ---- inner: runs once both nodes are ready ----
PIDS=()
cleanup() {
    trap - EXIT INT TERM
    local p
    for p in "${PIDS[@]:-}"; do
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then kill -TERM "$p" 2>/dev/null || true; fi
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 143' TERM INT

mapfile -t c < <(consumer_cmd AB "$EGRESS_B"); "${c[@]}" > "$OUT/video_AB_rx.log" 2>&1 < /dev/null & PIDS+=("$!"); CONS_AB=$!
mapfile -t c < <(consumer_cmd BA "$EGRESS_A"); "${c[@]}" > "$OUT/video_BA_rx.log" 2>&1 < /dev/null & PIDS+=("$!"); CONS_BA=$!
sleep 2
mapfile -t c < <(producer_cmd AB "$INGRESS_A" "${CAM_INPUT[@]}" -i "$CAMERA")
"${c[@]}" > "$OUT/video_AB_tx.log" 2>&1 < /dev/null & PROD_AB=$!; PIDS+=("$PROD_AB")
mapfile -t c < <(producer_cmd BA "$INGRESS_B" -re -f lavfi -i "$LAVFI_SRC")
"${c[@]}" > "$OUT/video_BA_tx.log" 2>&1 < /dev/null & PROD_BA=$!; PIDS+=("$PROD_BA")
wait "$PROD_AB" || echo "A->B producer exited non-zero (see video_AB_tx.log)" >&2
wait "$PROD_BA" || echo "B->A producer exited non-zero (see video_BA_tx.log)" >&2
sleep 3   # let the last bursts arrive
# A consumer blocked in a UDP read does not act on SIGTERM until a packet arrives; the URL's
# 5-s timeout ends it on its own, and SIGKILL is the fallback.
kill -TERM "$CONS_AB" "$CONS_BA" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8; do
    kill -0 "$CONS_AB" 2>/dev/null || kill -0 "$CONS_BA" 2>/dev/null || break
    sleep 1
done
kill -KILL "$CONS_AB" "$CONS_BA" 2>/dev/null || true
wait "$CONS_AB" "$CONS_BA" 2>/dev/null || true
PIDS=()

python_bin=${PYTHON:-/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python}
[ -x "$python_bin" ] || python_bin=python
"$python_bin" "$HERE/video_counts.py" "$OUT" > "$OUT/video_counts.json"
cat "$OUT/video_counts.json"

#!/usr/bin/env bash
# OTA webcam video demo:
#   ffmpeg -> UDP 52001 -> tx_app ~~RF~~ rx_app -> UDP 52012 -> ffplay
# at the default profile (configs/profiles/ota_2p45ghz.yaml) unless --overlay
# names another.  --transport pluto runs the same demo between two ADALM-Plutos.
# Run it inside the project's conda environment (conda activate ofdm-message-link).
# Opens the RX and TX windows, waits until the operator has chosen and started
# a radio in each (Hardware tab: device, antenna, gain, then Start radio), then
# starts ffplay and ffmpeg.  --auto-start skips the choice for scripted runs.
# Always stops in the reverse-safe order ffmpeg -> TX -> RX -> ffplay, also on Ctrl-C.
# Every log file is capped by rotation and only the newest --keep-runs default
# log directories are kept, so a demo left running for hours cannot fill the disk.
# Usage guide: docs/03-your-own-app.md
set -euo pipefail

usage() {
    cat <<'EOF'
usage: scripts/run_ota_video_demo.sh --enable-rf [options]

  --enable-rf          required: this script keys the transmitting radio
  --transport T        uhd (USRPs, default) or pluto (ADALM-Plutos)
  --overlay PATH       configuration overlay passed to both apps, repeatable
                       (default: the apps' default, configs/profiles/ota_2p45ghz.yaml)
  --duration SEC       stop automatically after SEC seconds of video (default: run until Ctrl-C)
  --out DIR            log directory (default: /tmp/ofdm_ota_video_demo/<timestamp>)
  --log-max-mb MB      cap for rx.log, tx.log, ffplay.log and ffmpeg.log; a full file is
                       rotated to NAME.1 (one old copy kept); 0 = no cap (default: 4)
  --stats-max-mb MB    the same cap for rx_stats.jsonl (default: 16, over an hour of refreshes)
  --keep-runs N        keep only the newest N run directories under
                       /tmp/ofdm_ota_video_demo, this one included; 0 = never delete
                       (default: 5; ignored with --out)
  --mcs-index N        TX MCS (default: 4; use 0 when RX effective SNR < 11 dB)
  --video-kbps N       H.264 bit rate (default: 1300; use 750 with --mcs-index 0)
  --auto-start         start both radios with the serials and gains below instead of
                       waiting for the operator to choose them in the windows;
                       needs --tx-serial and --rx-serial
  --tx-serial S        transmitter preselected in the TX window (uhd_find_devices lists
                       serials); pluto always needs both serials
  --rx-serial S        receiver preselected in the RX window
  --tx-gain DB         preselected TX gain (default: the device minimum; N210/CBX 0-31.5,
                       B210 0-89.75; a Pluto's is its attenuator, -89.75 to 0)
  --rx-gain DB         preselected RX gain (default: 15)
                       Gains stay adjustable in the windows while the radios run.
  --camera DEV         (default: /dev/video0)
  --layout MODE        window placement: auto|single|dual|none (default: auto)
                       dual: TX fills screen 1; screen 2 has video (top 2/3) over RX (bottom 1/3)
                       single: RX left half; video top-right; TX bottom-right
                       auto: dual with two or more screens, else single; none if xrandr fails
                       none: no placement, the window manager decides
EOF
}

ENABLE_RF=0
DURATION=""
OUT=""
MCS=4
VIDEO_KBPS=1300
TRANSPORT=uhd
TX_SERIAL="" RX_SERIAL="" TX_GAIN="" RX_GAIN=""
OVERLAYS=()
CAMERA=/dev/video0
LAYOUT=auto
AUTO_START=0
LOG_MAX_MB=4
STATS_MAX_MB=16
KEEP_RUNS=5
RUNS_DIR=/tmp/ofdm_ota_video_demo
# How long --auto-start may take to probe and start each radio.
AUTO_START_TIMEOUT=60
while [ $# -gt 0 ]; do
    case "$1" in
        --enable-rf) ENABLE_RF=1 ;;
        --auto-start) AUTO_START=1 ;;
        --duration) DURATION="$2"; shift ;;
        --out) OUT="$2"; shift ;;
        --transport) TRANSPORT="$2"; shift ;;
        --overlay) OVERLAYS+=(--overlay "$2"); shift ;;
        --mcs-index) MCS="$2"; shift ;;
        --video-kbps) VIDEO_KBPS="$2"; shift ;;
        --tx-serial) TX_SERIAL="$2"; shift ;;
        --rx-serial) RX_SERIAL="$2"; shift ;;
        --tx-gain) TX_GAIN="$2"; shift ;;
        --rx-gain) RX_GAIN="$2"; shift ;;
        --camera) CAMERA="$2"; shift ;;
        --layout) LAYOUT="$2"; shift ;;
        --log-max-mb) LOG_MAX_MB="$2"; shift ;;
        --stats-max-mb) STATS_MAX_MB="$2"; shift ;;
        --keep-runs) KEEP_RUNS="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if [ "$ENABLE_RF" -ne 1 ]; then
    echo "refusing to transmit: pass --enable-rf to acknowledge that this keys the transmitter" >&2
    exit 2
fi
case "$LAYOUT" in
    auto|single|dual|none) ;;
    *) echo "--layout must be auto, single, dual or none, not '$LAYOUT'" >&2; exit 2 ;;
esac
# A USRP front end is chosen by channel and antenna port; a Pluto has one of each.
case "$TRANSPORT" in
    uhd)
        : "${RX_GAIN:=15}"
        FRONT_END=(--channel 0 --antenna TX/RX) ;;
    pluto)
        if [ -z "$TX_SERIAL" ] || [ -z "$RX_SERIAL" ]; then
            echo "--transport pluto needs --tx-serial and --rx-serial (two different Plutos)" >&2
            exit 2
        fi
        : "${RX_GAIN:=15}"
        FRONT_END=() ;;
    *) echo "--transport must be uhd or pluto, not '$TRANSPORT'" >&2; exit 2 ;;
esac
if [ "$AUTO_START" -eq 1 ] && { [ -z "$TX_SERIAL" ] || [ -z "$RX_SERIAL" ]; }; then
    echo "--auto-start needs --tx-serial and --rx-serial" >&2
    exit 2
fi
# Only what the operator gave is preselected; the windows choose the rest.
TX_SELECT=() RX_SELECT=()
[ -n "$TX_SERIAL" ] && TX_SELECT+=(--serial "$TX_SERIAL")
[ -n "$RX_SERIAL" ] && RX_SELECT+=(--serial "$RX_SERIAL")
[ -n "$TX_GAIN" ] && TX_SELECT+=(--gain "$TX_GAIN")
RX_SELECT+=(--gain "$RX_GAIN")

for value in "$LOG_MAX_MB" "$STATS_MAX_MB"; do
    [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] \
        || { echo "--log-max-mb and --stats-max-mb take a size in MiB, not '$value'" >&2; exit 2; }
done
[[ "$KEEP_RUNS" =~ ^[0-9]+$ ]] || { echo "--keep-runs takes a count, not '$KEEP_RUNS'" >&2; exit 2; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PRUNE_RUNS=0
if [ -z "$OUT" ]; then
    OUT="$RUNS_DIR/$(date +%Y%m%d-%H%M%S)"
    PRUNE_RUNS=1
fi
PYTHON="${OFDM_PYTHON:-python}"
# MPEG-TS mux rate: video rate plus about 11% for audio-less TS/PES overhead.
MUX_KBPS=$(( VIDEO_KBPS * 111 / 100 ))

if ! "$PYTHON" -c "import ofdm_message_link" 2>/dev/null; then
    echo "cannot import ofdm_message_link: run 'conda activate ofdm-message-link' first" >&2
    exit 1
fi
if pgrep -f '^(python|\S*/python) -m ofdm_message_link[.](tx_app|rx_app)' >/dev/null \
    || pgrep -f '^ffmpeg .*52001' >/dev/null || pgrep -f '^ffplay .*52012' >/dev/null; then
    echo "an earlier demo is still running; stop it first (see docs/03-your-own-app.md)" >&2
    exit 1
fi

mkdir -p "$OUT"
cd "$REPO"

# Old runs: only timestamp-named directories under $RUNS_DIR are candidates;
# an --out directory is never touched.
if [ "$PRUNE_RUNS" -eq 1 ]; then
    "$PYTHON" -m ofdm_message_link.log_limits \
        prune-runs --keep "$KEEP_RUNS" --current "$OUT" "$RUNS_DIR"
fi

# capped_log FILE: stdin -> FILE, rotated to FILE.1 at --log-max-mb.
capped_log() {
    exec "$PYTHON" -m ofdm_message_link.log_limits \
        copy --max-mb "$LOG_MAX_MB" --backups 1 "$1"
}
RX_PID="" TX_PID="" PLAY_PID="" FF_PID=""

# Window placement: src/ofdm_message_link/window_layout.py turns the
# connected screens into one WxH+X+Y per window (X+Y = frame corner, WxH =
# client area).  Empty arrays leave every command exactly as with no layout.
RX_GEOM=() TX_GEOM=() PLAY_GEOM=()
LAYOUT_PLAN="$(xrandr --query 2>/dev/null \
    | "$PYTHON" -m ofdm_message_link.window_layout "$LAYOUT" \
        --workareas "$(xprop -root _GTK_WORKAREAS_D0 2>/dev/null || true)")" \
    || LAYOUT_PLAN="layout none"
while read -r role geometry; do
    case "$role" in
        layout) LAYOUT="$geometry" ;;
        tx) TX_GEOM=(--geometry "$geometry") ;;
        rx) RX_GEOM=(--geometry "$geometry") ;;
        video)
            IFS='x+' read -r w h x y <<< "$geometry"
            PLAY_GEOM=(-left "$x" -top "$y" -x "$w" -y "$h") ;;
    esac
done <<< "$LAYOUT_PLAN"
echo "window layout: $LAYOUT"

stop_one() {  # pid seconds
    [ -n "$1" ] && kill -0 "$1" 2>/dev/null || return 0
    kill -TERM "$1" 2>/dev/null || true
    for _ in $(seq $(( $2 * 10 ))); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done
    kill -KILL "$1" 2>/dev/null || true
}
cleanup() {
    trap - EXIT INT TERM
    echo "stopping: ffmpeg -> TX -> RX -> ffplay"
    stop_one "$FF_PID" 3
    stop_one "$TX_PID" 8
    stop_one "$RX_PID" 8
    stop_one "$PLAY_PID" 2
    echo "logs: $OUT"
}
# A signal must end the script after cleanup; otherwise bash resumes where it
# was, e.g. still waiting for a radio that cleanup has just stopped.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

RX_READY="$OUT/rx.ready" TX_READY="$OUT/tx.ready"
START_ARGS=()
[ "$AUTO_START" -eq 1 ] && START_ARGS=(--auto-start)

# wait_ready LABEL FILE PID [TIMEOUT]: until the app reports its radio running.
wait_ready() {
    local waited=0
    until [ -f "$2" ]; do
        if ! kill -0 "$3" 2>/dev/null; then
            echo "$1 app exited before its radio started; see $OUT/${1,,}.log" >&2
            exit 1
        fi
        if [ -n "${4:-}" ] && [ "$waited" -ge $(( $4 * 2 )) ]; then
            echo "$1 radio did not start within $4 s; see $OUT/${1,,}.log" >&2
            exit 1
        fi
        sleep 0.5
        waited=$(( waited + 1 ))
    done
    echo "  $1 radio on: $(cat "$2")"
}

echo "[1/4] RX app -> UDP 52012 (preselected ${RX_SERIAL:-none}, ${RX_GAIN} dB)"
"$PYTHON" -m ofdm_message_link.rx_app \
    "${OVERLAYS[@]}" --transport "$TRANSPORT" "${RX_SELECT[@]}" "${FRONT_END[@]}" \
    "${START_ARGS[@]}" --ready-file "$RX_READY" \
    --egress-port 52012 --stats-log "$OUT/rx_stats.jsonl" \
    --stats-log-max-mb "$STATS_MAX_MB" --stats-log-backups 1 "${RX_GEOM[@]}" \
    > >(capped_log "$OUT/rx.log") 2>&1 < /dev/null &
RX_PID=$!
# Auto-start keeps the RX-first order: RX must be listening before TX keys up.
[ "$AUTO_START" -eq 1 ] && wait_ready RX "$RX_READY" "$RX_PID" "$AUTO_START_TIMEOUT"

echo "[2/4] TX app, MCS $MCS (preselected ${TX_SERIAL:-none}, ${TX_GAIN:-minimum} dB); RF is authorised"
"$PYTHON" -m ofdm_message_link.tx_app \
    "${OVERLAYS[@]}" --transport "$TRANSPORT" "${TX_SELECT[@]}" "${FRONT_END[@]}" \
    "${START_ARGS[@]}" --ready-file "$TX_READY" --mcs-index "$MCS" "${TX_GEOM[@]}" \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF' \
    > >(capped_log "$OUT/tx.log") 2>&1 < /dev/null &
TX_PID=$!

if [ "$AUTO_START" -eq 1 ]; then
    wait_ready TX "$TX_READY" "$TX_PID" "$AUTO_START_TIMEOUT"
else
    echo "      In each window's Hardware tab choose Device, Front end, Antenna and Gain,"
    echo "      then press Start radio (RX first).  Center and Sample rate must match in"
    echo "      both windows.  Video starts once both radios are on; Ctrl-C cancels."
    wait_ready RX "$RX_READY" "$RX_PID"
    wait_ready TX "$TX_READY" "$TX_PID"
fi

# -nostats: ffplay and ffmpeg otherwise rewrite a status line about once a
# second, which is most of what their logs would hold; warnings still appear.
echo "[3/4] ffplay on UDP 52012"
ffplay -nostats -fflags nobuffer -flags low_delay -framedrop -window_title "OTA RX video" "${PLAY_GEOM[@]}" \
    "udp://127.0.0.1:52012?fifo_size=100000&overrun_nonfatal=1" \
    > >(capped_log "$OUT/ffplay.log") 2>&1 < /dev/null &
PLAY_PID=$!
sleep 2

echo "[4/4] ffmpeg $CAMERA -> ${VIDEO_KBPS}k H.264 -> UDP 52001"
# -nostdin: a background ffmpeg that reads the terminal is stopped (state T) and sends nothing.
ffmpeg -nostdin -nostats -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15 -i "$CAMERA" \
    -vf format=yuv420p -c:v libx264 -preset ultrafast -tune zerolatency \
    -b:v "${VIDEO_KBPS}k" -maxrate "${VIDEO_KBPS}k" -minrate "${VIDEO_KBPS}k" \
    -bufsize "$(( VIDEO_KBPS / 2 ))k" \
    -x264-params "nal-hrd=cbr:force-cfr=1:intra-refresh=1:keyint=30:repeat-headers=1" \
    -muxrate "${MUX_KBPS}k" -f mpegts "udp://127.0.0.1:52001?pkt_size=752" \
    > >(capped_log "$OUT/ffmpeg.log") 2>&1 < /dev/null &
FF_PID=$!

echo "streaming; logs in $OUT  (Ctrl-C to stop)"
if [ -n "$DURATION" ]; then
    # In the background: bash runs a trap only after a foreground command
    # returns, so a plain sleep would hold a Ctrl-C or TERM until it ended.
    sleep "$DURATION" &
    wait $! || true
else
    wait "$FF_PID" || true
fi

#!/usr/bin/env bash
# Measurement matrices M2 (fixed MCS) and M3 (link adaptation with a gain step) of
# design_duplex.md section 6.3, each run through run_duplex.sh (which takes the
# hardware lock and the load gate per run).
#
#   run_matrix.sh fixed|la|all --out-root DIR --enable-rf [--dry-run] [options] [-- run_duplex args]
set -euo pipefail

# ---- the matrices; edit here ----
# M2: "MCS GAIN_DB RATE_PER_DIRECTION" -> duplex + two one-way references (reverse idle)
M2_POINTS=("12 -5 450" "12 -12.5 450" "12 -20 450" "8 -20 180")
M2_DURATION=60
# M2 extra when SNR at -20 dB is below T(8) = 9 dB: gain dip -5 -> -15 -> -5 (see --m2-dip)
M2_DIP_PROFILE="0:-5,30:-15,60:-5"
# M3: node A (1) steps its TX gain, node B stays at -5 dB
M3_PROFILE="0:-5,30:-20,60:-5"
M3_DURATION=90
M3_RATE_AB=555      # 4 Mb/s at 900 B
M3_RATE_BA=139      # 1 Mb/s at 900 B (feedback piggybacks)
M3_B_GAIN=-5
M3_OLLA_REPEATS=2
M3_FIXED_MCS=(8 10 12 14)
LA_SPEC=olla
SIZE=900
# ---------------------------------

usage() {
    cat <<'EOF'
usage: experiments/duplex_fdd/run_matrix.sh MATRIX --out-root DIR [--enable-rf] [--dry-run] [options] [-- run_duplex args]

  MATRIX            fixed (M2), la (M3) or all
  --out-root DIR    run directories are created under DIR (default /tmp/duplex_matrix/<timestamp>)
  --dry-run         print the plan (one line per run, with the run_duplex.sh command) and exit
  --enable-rf       passed to run_duplex.sh (required for a real run)
  --workers N       decode workers per node (default: run_duplex.sh default)
  --m2-dip          add the -5 -> -15 -> -5 M2 variant at MCS 8 and 12 (when SNR at -20 dB < 9 dB)
  --skip-existing   skip runs whose DIR/meta.json and load_*.json exist
  everything after -- goes to every run_duplex.sh call (e.g. --serial1 ...).
EOF
}

MATRIX="" OUT_ROOT="" DRY=0 RF=() WORKERS=() DIP=0 SKIP=0 PASS=()
while [ $# -gt 0 ]; do
    case "$1" in
        fixed|la|all) MATRIX="$1" ;;
        --out-root) OUT_ROOT="$2"; shift ;;
        --dry-run) DRY=1 ;;
        --enable-rf) RF=(--enable-rf) ;;
        --workers) WORKERS=(--decode-workers "$2"); shift ;;
        --m2-dip) DIP=1 ;;
        --skip-existing) SKIP=1 ;;
        --) shift; PASS=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[ -n "$MATRIX" ] || { usage >&2; exit 2; }
[ -n "$OUT_ROOT" ] || OUT_ROOT=/tmp/duplex_matrix/$(date +%Y%m%d-%H%M%S)
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/run_duplex.sh"
if [ "$DRY" -ne 1 ] && [ "${#RF[@]}" -eq 0 ]; then echo "pass --enable-rf (or --dry-run)" >&2; exit 2; fi

N=0
do_run() {  # name, run_duplex args...
    local name=$1; shift
    local dir="$OUT_ROOT/$name"
    N=$(( N + 1 ))
    local cmd=("$RUN" --out "$dir" --size "$SIZE" "${RF[@]}" "${WORKERS[@]}" "$@" "${PASS[@]}")
    if [ "$DRY" -eq 1 ]; then
        printf '%02d %s\n     %s\n' "$N" "$name" "${cmd[*]}"
        return 0
    fi
    if [ "$SKIP" -eq 1 ] && [ -f "$dir/meta.json" ] && ls "$dir"/load_*.json >/dev/null 2>&1; then
        echo "skip $name (done)"; return 0
    fi
    echo "=== $N $name"
    "${cmd[@]}" || echo "run $name FAILED (exit $?); continuing" >&2
}

if [ "$MATRIX" = fixed ] || [ "$MATRIX" = all ]; then
    for point in "${M2_POINTS[@]}"; do
        read -r mcs gain rate <<< "$point"
        tag="m2_mcs${mcs}_g${gain}"
        common=(--mcs "$mcs" --la fixed --tx-gain "$gain" --duration "$M2_DURATION")
        do_run "${tag}_duplex" "${common[@]}" --rate-ab "$rate" --rate-ba "$rate"
        do_run "${tag}_oneway_AB" "${common[@]}" --rate-ab "$rate" --rate-ba 0
        do_run "${tag}_oneway_BA" "${common[@]}" --rate-ab 0 --rate-ba "$rate"
    done
    if [ "$DIP" -eq 1 ]; then
        for point in "12 450" "8 180"; do
            read -r mcs rate <<< "$point"
            do_run "m2dip_mcs${mcs}_duplex" --mcs "$mcs" --la fixed --tx-gain -5 \
                --gain-profile "$M2_DIP_PROFILE" --duration 90 --rate-ab "$rate" --rate-ba "$rate"
        done
    fi
fi

if [ "$MATRIX" = la ] || [ "$MATRIX" = all ]; then
    m3=(--gain-profile1 "$M3_PROFILE" --tx-gain1 "$M3_B_GAIN" --tx-gain2 "$M3_B_GAIN"
        --duration "$M3_DURATION")
    for i in $(seq "$M3_OLLA_REPEATS"); do
        do_run "m3_${LA_SPEC}_run$i" "${m3[@]}" --la "$LA_SPEC" --mcs 8 \
            --rate-ab "$M3_RATE_AB" --rate-ba "$M3_RATE_BA"
    done
    do_run "m3_${LA_SPEC}_ba_idle" "${m3[@]}" --la "$LA_SPEC" --mcs 8 \
        --rate-ab "$M3_RATE_AB" --rate-ba 0
    for mcs in "${M3_FIXED_MCS[@]}"; do
        do_run "m3_fixed${mcs}" "${m3[@]}" --la fixed --mcs "$mcs" \
            --rate-ab "$M3_RATE_AB" --rate-ba "$M3_RATE_BA"
    done
fi
echo "planned/finished $N runs under $OUT_ROOT"

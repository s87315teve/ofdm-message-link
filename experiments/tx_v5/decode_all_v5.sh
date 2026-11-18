#!/usr/bin/env bash
# Decode every phase-1 recording with the current tree (sequential, one job at a time).
#   bash experiments/tx_v5/decode_all_v5.sh [--dry-run]   (cwd = repo root)
#
# CAPTURES is the directory holding caps_v5/ and caps_baseline*/ (the raw IQ is not
# in the repo); PYTHON the interpreter.  --dry-run prints the commands only.
#
# Since v2.0 the receiver finds only wire v5 unless phy.rx.legacy_acquisition is
# true.  The baseline recordings are the legacy (v1) waveform, so the two
# legacy_on_new_rx* steps set it explicitly; every other step decodes v5 with the
# defaults.
set -euo pipefail
DRY=0
case "${1:-}" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac
S=${CAPTURES:-/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad}
PY=${PYTHON:-/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python}
OUT=experiments/tx_v5/results/v5
LEGACY=(--set phy.rx.legacy_acquisition=true)
export PYTHONPATH=src OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
DEC=("$PY" experiments/tx_v5/offline_decode_v5.py)

run() {  # LOG CMD...: run CMD with stdout to LOG (+LOG appends), or print it with --dry-run
    local log=$1; shift
    if [ "$DRY" -eq 1 ]; then echo "$* > ${log#+}"
    elif [ "${log:0:1}" = + ]; then "$@" >> "${log#+}"
    else "$@" > "$log"; fi
}

if [ "$DRY" -eq 0 ]; then
    [ -d "$S/caps_v5" ] || { echo "no captures in $S (set CAPTURES)" >&2; exit 1; }
    echo "git HEAD: $(git rev-parse HEAD)  start $(date +%T)"
fi

run $OUT/logs/ota_v5.log "${DEC[@]}" $S/caps_v5/sweep --out $OUT/ota_v5.json
run $OUT/logs/legacy.log "${DEC[@]}" $S/caps_baseline $S/caps_baseline_extra \
    --out $OUT/legacy_on_new_rx.json "${LEGACY[@]}"
run $OUT/logs/legacy_v5off.log "${DEC[@]}" $S/caps_baseline $S/caps_baseline_extra \
    --out $OUT/legacy_on_new_rx_v5acq_off.json "${LEGACY[@]}" --set phy.rx.v5_acquisition=false
run $OUT/logs/guards.log "${DEC[@]}" $S/caps_v5/guards --out $OUT/guards_low_snr.json
run $OUT/logs/scrambler.log "${DEC[@]}" $S/caps_v5/scrambler --out $OUT/scrambler_ota.json

ABL=()
for g in -28p75 -27p50 -26p25 -25p00; do ABL+=("$S/caps_v5/sweep/v5m8_tx${g}.npy"); done
for g in -23p75 -22p50 -21p25 -20p00; do ABL+=("$S/caps_v5/sweep/v5m12_tx${g}.npy"); done
[ "$DRY" -eq 1 ] || rm -f $OUT/rx_ablation.json $OUT/logs/ablation.log
for set in "" phy.rx.ltf_average=false phy.rx.channel_smoothing=false \
           phy.rx.llr_noise_source=header phy.rx.llr_noise_source=ltf; do
    if [ -z "$set" ]; then
        run +$OUT/logs/ablation.log "${DEC[@]}" "${ABL[@]}" --out $OUT/rx_ablation.json --append
    else
        run +$OUT/logs/ablation.log "${DEC[@]}" "${ABL[@]}" --out $OUT/rx_ablation.json \
            --append --set "$set"
    fi
done

NOISE=("$PY" experiments/tx_v5/noise_scan_v5.py analyze --dir $S/caps_v5/noise --prefilter
       --out $OUT/noise_scan.json)
if [ "$DRY" -eq 1 ]; then echo "${NOISE[*]} > $OUT/logs/noise_scan.log 2>&1"
else "${NOISE[@]}" > $OUT/logs/noise_scan.log 2>&1; fi
[ "$DRY" -eq 1 ] || echo "done $(date +%T)"

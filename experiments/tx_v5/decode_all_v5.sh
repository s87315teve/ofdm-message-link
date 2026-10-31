#!/usr/bin/env bash
# Decode every phase-1 recording with the current tree (sequential, one job at a time).
#   bash experiments/tx_v5/decode_all_v5.sh   (cwd = repo root)
set -euo pipefail
S=/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad
PY=/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python
OUT=experiments/tx_v5/results/v5
export PYTHONPATH=src OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
DEC="$PY experiments/tx_v5/offline_decode_v5.py"
echo "git HEAD: $(git rev-parse HEAD)  start $(date +%T)"

$DEC $S/caps_v5/sweep --out $OUT/ota_v5.json > $OUT/logs/ota_v5.log
$DEC $S/caps_baseline $S/caps_baseline_extra --out $OUT/legacy_on_new_rx.json > $OUT/logs/legacy.log
$DEC $S/caps_baseline $S/caps_baseline_extra --out $OUT/legacy_on_new_rx_v5acq_off.json \
    --set phy.rx.v5_acquisition=false > $OUT/logs/legacy_v5off.log
$DEC $S/caps_v5/guards --out $OUT/guards_low_snr.json > $OUT/logs/guards.log
$DEC $S/caps_v5/scrambler --out $OUT/scrambler_ota.json > $OUT/logs/scrambler.log

ABL=()
for g in -28p75 -27p50 -26p25 -25p00; do ABL+=("$S/caps_v5/sweep/v5m8_tx${g}.npy"); done
for g in -23p75 -22p50 -21p25 -20p00; do ABL+=("$S/caps_v5/sweep/v5m12_tx${g}.npy"); done
rm -f $OUT/rx_ablation.json
for set in "" phy.rx.ltf_average=false phy.rx.channel_smoothing=false \
           phy.rx.llr_noise_source=header phy.rx.llr_noise_source=ltf; do
    if [ -z "$set" ]; then $DEC "${ABL[@]}" --out $OUT/rx_ablation.json --append
    else $DEC "${ABL[@]}" --out $OUT/rx_ablation.json --append --set "$set"; fi
done > $OUT/logs/ablation.log

$PY experiments/tx_v5/noise_scan_v5.py analyze --dir $S/caps_v5/noise --prefilter \
    --out $OUT/noise_scan.json > $OUT/logs/noise_scan.log 2>&1
echo "done $(date +%T)"

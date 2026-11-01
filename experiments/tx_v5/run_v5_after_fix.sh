#!/usr/bin/env bash
# Re-measure live goodput points after the RX-acquisition and min-burst-interval fixes.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONPATH=src
cd /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5
R=$PWD/results/v5_goodput_after_fix
ROOT=/home/steve/Desktop/steve/my_github/ofdm-message-link
PY=/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python
# name gain mcs size est extra
while read -r name g m s e x; do
  d=$R/$name
  echo "$(date +%T) start $name" >> $R/driver.log
  for try in 1 2 3; do
    args=(--root $ROOT --tx-gain $g --mcs $m --size $s --est-rate $e --out $d)
    [ -n "$x" ] && args+=(--tx-arg "$x")
    $PY goodput_search.py "${args[@]}" >> $R/driver.log 2>&1 && grep -q '"result"' $d/search.json 2>/dev/null && break
    echo "$(date +%T) retry $name" >> $R/driver.log
  done
done <<'L'
p1_300B_m14_-5 -5 14 300 2468
p2_900B_m16_-5 -5 16 900 600
p2_900B_m17_-5 -5 17 900 600
p3_900B_m14_-5 -5 14 900 990
p3_900B_m12_-15 -15 12 900 658
p4_300B_m12_-15 -15 12 300 1772
p4_900B_m14_-5_aggoff -5 14 900 274 --aggregation off
L
echo "$(date +%T) ALLDONE" >> $R/driver.log

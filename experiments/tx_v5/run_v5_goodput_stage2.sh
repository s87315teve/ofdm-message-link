#!/usr/bin/env bash
# Stage 2/3: aggregation off (900 B) and 300 B aggregation on, for the best MCS at each gain.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
cd /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5
R=$PWD/results/v5_goodput
P=/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python
M=/home/steve/Desktop/steve/my_github/ofdm-message-link
run() { # name gain mcs size est extra...
  n=$1; g=$2; m=$3; s=$4; e=$5; shift 5
  d=$R/$n/${g}_mcs${m}
  [ -f $d/search.json ] && grep -q '"result"' $d/search.json && return
  echo "$(date +%T) start $n $g mcs$m" >> $R/driver2.log
  $P goodput_search.py --root $M --tx-gain $g --mcs $m --size $s --est-rate $e "$@" --out $d >> $R/driver2.log 2>&1
}
run 900B_agg_off -5 14 900 500 --tx-arg "--aggregation off"
run 900B_agg_off -15 12 900 400 --tx-arg "--aggregation off"
run 300B_agg_on -5 14 300 2500
run 300B_agg_on -15 12 300 1800
echo "$(date +%T) ALLDONE" >> $R/driver2.log

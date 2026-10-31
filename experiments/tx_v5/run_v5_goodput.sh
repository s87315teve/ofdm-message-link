#!/usr/bin/env bash
# v5 goodput searches, 900 B, aggregation on.  Usage: run_v5_goodput.sh "GAIN MCS" ...
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
cd /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5
R=$PWD/results/v5_goodput
declare -A EST=([8]=350 [9]=520 [10]=700 [12]=900 [13]=1100 [14]=1100 [15]=1000 [16]=1200 [17]=1300)
for pair in "$@"; do
  set -- $pair
  d=$R/900B_agg_on/${1}_mcs${2}
  [ -f $d/search.json ] && grep -q '"result"' $d/search.json && continue
  echo "$(date +%T) start $1 mcs$2" >> $R/driver.log
  /home/steve/anaconda3/envs/gnuradio-ofdm/bin/python goodput_search.py --root /home/steve/Desktop/steve/my_github/ofdm-message-link --tx-gain $1 --mcs $2 --est-rate ${EST[$2]} --out $d >> $R/driver.log 2>&1
done
echo "$(date +%T) ALLDONE" >> $R/driver.log

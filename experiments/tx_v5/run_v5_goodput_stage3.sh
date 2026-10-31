#!/usr/bin/env bash
# Extra: 300 B with aggregation max-wait; then H7 (legacy improve-rx TX -> v5 RX).
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHON=/home/steve/anaconda3/envs/gnuradio-ofdm/bin/python
cd /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5
R=$PWD/results/v5_goodput
M=/home/steve/Desktop/steve/my_github/ofdm-message-link
export TX_ROOT_BASE=/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad/base
d=$R/300B_wait4ms/-5_mcs14
echo "$(date +%T) start wait4 300B -5 mcs14" >> $R/driver3.log
$PYTHON goodput_search.py --root $M --tx-gain -5 --mcs 14 --size 300 --est-rate 1500 --tx-arg "--aggregation-max-wait-ms 4" --out $d >> $R/driver3.log 2>&1
for spec in "-15 0 200 a" "-15 0 200 b" "-5 4 300 a" "-5 4 300 b"; do
  set -- $spec
  while [ "$(cut -d. -f1 /proc/loadavg)" -ge 4 ]; do sleep 10; done
  echo "$(date +%T) start H7 legacy mcs$2 gain $1 rate $3 $4 load $(cut -d' ' -f1 /proc/loadavg)" >> $R/driver3.log
  TX_ROOT=$TX_ROOT_BASE ./live_run.sh $M $1 $2 $3 30 $R/h7/mcs$2_g$1_$4 900 >> $R/driver3.log 2>&1
done
echo "$(date +%T) ALLDONE" >> $R/driver3.log

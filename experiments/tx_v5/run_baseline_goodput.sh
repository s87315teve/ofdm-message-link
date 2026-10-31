#!/usr/bin/env bash
# Baseline goodput searches (ROOT = improve-rx worktree), most important first.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
cd /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5
BASE=/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad/base
for pair in "-5 0" "-5 4" "-15 0" "-15 4" "-5 5" "-5 6" "-5 7" "-5 3" "-5 2" "-5 1" "-15 1" "-15 2" "-15 3" "-15 5" "-15 6" "-15 7"; do
  set -- $pair
  d=/home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5/results/baseline/goodput/${1}_mcs${2}
  [ -f $d/search.json ] && grep -q '"result"' $d/search.json && continue
  /home/steve/anaconda3/envs/gnuradio-ofdm/bin/python goodput_search.py --root $BASE --tx-gain $1 --mcs $2 --out $d >> /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5/results/baseline/goodput_driver.log 2>&1
done
echo ALLDONE >> /home/steve/Desktop/steve/my_github/ofdm-message-link/experiments/tx_v5/results/baseline/goodput_driver.log

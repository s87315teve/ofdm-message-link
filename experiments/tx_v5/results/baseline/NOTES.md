# Baseline (improve-rx @ 3ce3228) on two PlutoSDRs

Date: 2026-11-01, 02:50-03:36 local time (UTC+8); captures 02:50-02:54, goodput searches until 03:36. Placement unchanged during the whole session. 2.45 GHz, 5 MS/s, RX gain 15.
Baseline tree: detached worktree at 3ce3228 (BASE under the session scratchpad), run with PYTHONPATH=src, cwd=BASE.
All Python with OMP/OPENBLAS/MKL_NUM_THREADS=1, one heavy job at a time.

## Commands
- Capture (per MCS, 13 gains -30..0 step 2.5, 400 bursts, 100/s, 900 B):
  `PYTHONPATH=src python experiments/rx_improvement/ota_capture.py --out caps_baseline --mcs {qpsk,qam16} --tx-gains -30 -27.5 ... 0 --enable-rf --acknowledgement '...'`
- Extra point: qpsk -18.75 dB (crossing region), same settings, captured after the sweep (about 20 min later).
- Decode: `PYTHONPATH=src python experiments/rx_improvement/offline_decode.py caps_baseline ota_baseline.json` (cwd=BASE).
- Goodput: `run_baseline_goodput.sh` -> `goodput_search.py --root BASE --tx-gain G --mcs M --out goodput/<G>_mcs<M>` (size 900), then
  `--size 300` for -5 dB/MCS 4 and -15 dB/MCS 0. Each run = `live_run.sh` 30 s, rx_app/tx_app both from BASE.

## Notes / anomalies
- No reruns triggered (no fail coincided with an overflow; no overflow-while-loaded in any run).
- The 1-min load at run start was 0.7-4.0. Most of it is the apps' own decode workers from the previous run still averaging in, not
  other agents; the gate (<4) was never hit, but the 300 B -5 dB MCS 4 search had starts at 3.6-3.99 (borderline, still no overflow).
- Worker CPU = mean %CPU per decode-worker process (8 workers), from /proc tick deltas over the load window.
- Search criterion is strict: loss < 0.1% means 0 lost for fewer than 1000 datagrams, so results at the pass/fail edge are noisy
  (e.g. 224.5/s with MCS 0 lost 0.15% in one run, 213.8/s passed once and lost 0.08% in another).
- Search resolution is about 5%. MCS 1/2/3/5/6/7 (convolutional / uncoded entries) get 0 or very low goodput at 22 dB SNR with 900 B:
  uncoded entries lose 20%+ or everything (MCS 7 delivered 0). Not investigated; they are the baseline's behaviour.
- At -15 dB, MCS 4 (SNR 11.3 dB) lost 1.8% at 20/s -> goodput 0 by the rule (probe loss > 1%); consistent with the capture sweep (1.0% loss at -15).
- The Pluto TX saturates near 225/s (MCS 0) and 335/s (MCS 4) for 900 B; with 300 B the ceiling is higher (400-490/s).
- Raw IQ is in scratchpad caps_baseline/ (not copied); per-capture metadata in captures/.
- Scripts: experiments/tx_v5/{live_run.sh,goodput_search.py,cpu_sampler.py,run_baseline_goodput.sh} (untracked, tree-agnostic via ROOT;
  live_run.sh uses ../ota_udp_throughput/udp_load.py from the tx_v5 tree's own experiments dir).

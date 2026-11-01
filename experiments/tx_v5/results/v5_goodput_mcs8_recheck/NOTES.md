# v5 goodput recheck after min_burst_interval (HEAD f3f84fc), 900 B, TX -15 dB, RX gain 15

Runs: goodput_search.py (30 s/run, pass = loss < 0.1% and RX overflow while loaded = 0). Data: recheck.json, */search.json.
RX overflow while loaded was 0 in every run; 1-min load at start 0.75-3.72 (< 4).

| case | before (no interval) | after (4 ms start-to-start) | notes |
|---|---|---|---|
| MCS 8 | 243/s, 1.748 Mb/s | 252.8/s, 1.820 Mb/s (259.7 fails at 0.33%) | ~2.0 subframes/burst at the limit; no regression |
| MCS 10 | 486/s, 3.496 Mb/s | 283.1/s, 2.038 Mb/s (291.6 fails at 0.103%, marginal) | REGRESSION -42%; only 1.36-1.42 subframes/burst, p50 latency 46 ms |
| MCS 8, interval 0 | (243/s ref) | 218.7/s, 1.575 Mb/s (225.5 fails at 1.0%; 246 at 0.11%) | worse than default 4 ms: the interval helps MCS 8 (more aggregation, fewer bursts) |

Not measured: MCS 10 with interval 0 (out of time budget), which would show whether the 4 ms interval is what caps MCS 10.
Note: the first interval-0 attempt errored on argparse; the working form is `--tx-arg="--min-burst-interval-ms 0"`.

## Direct runs after the search (commander, same day, tx-v5 f3f84fc)

The MCS 10 search above stopped at 283/s on a marginal 0.103% loss. Direct 30-s runs at the
first-round rate, 486/s, 900 B, TX -15 dB (`live_run.sh`, `TX_EXTRA_ARGS="--min-burst-interval-ms N"`):

| run (order) | interval | loss | goodput | subframes/PSDU | PSDU FCS fails |
|---|---|---|---|---|---|
| direct_mcs10_486/int4 (1st) | 4 ms | 3.573% | 3.374 Mb/s | 3.58 | 193 |
| direct_mcs10_486/int0 (2nd) | 0 | 0.000% | 3.499 Mb/s | 3.72 | 0 |
| direct_mcs10_486/rep_int0 (3rd) | 0 | 0.000% | 3.499 Mb/s | 3.83 | 0 |
| direct_mcs10_486/rep_int4 (4th) | 4 ms | 0.021% | 3.499 Mb/s | 3.84 | 1 |

All losses were on air (FCS failures), with 0 RX overflow. The first run is an outlier that the repeat in
reversed order did not reproduce; at 3.6 subframes per PSDU the bursts are about 7.7 ms long, longer than
the interval, so the interval does not even hold them back. Run-to-run variation in this band (Wi-Fi) is
larger than any interval effect here. Conclusion: MCS 10 at -15 dB still passes 486/s (3.50 Mb/s); the
283/s search result is a search-noise stop, not a regression.

High-rate points with the interval off, TX -5 dB (`direct_int0_check/`): 300 B MCS 14 at 2568/s: 0% loss,
6.163 Mb/s, 15.0 subframes/PSDU with interval 0 vs 15.4 with 4 ms; 900 B MCS 17 at 1291/s: 0.031% vs
0.010%, 9.29 Mb/s both. After the receiver fix the interval is no longer needed for these, but it does no
harm, and the MCS 8 search above went further with it (252.8/s vs 218.7/s). The 4 ms default stays.

# M3 run made before the minimum-burst-interval fix (kept for reference only)

`m3_olla_run1` ran with git 073ca15, before tx-v5 f3f84fc. The 4-ms minimum burst interval was
counted from the end of the previous push, so every burst was followed by at least 4 ms of idle
air. That capped the TX airtime (MCS 8 at ~0.67, see results/m2) and with it the A->B goodput.
The M3 results are in `../m3/`, all measured with the fixed code. A second pre-fix run was stopped
partway and deleted.

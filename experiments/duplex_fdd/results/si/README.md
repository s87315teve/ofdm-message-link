# M0/M1: USB capacity and self-interference of a Pluto in FDD

Two ADALM-Plutos (fw v0.39, AD9364): `…6e77` = node 1 / A, `…f9c6` = node 2 / B. Both and the
webcam share USB 2.0 bus 3 (480 Mb/s); `…6e77` is on a root port, `…f9c6` behind a hub
(`lsusb -t` in each `meta.json`). 5 MS/s, `ensm_mode` = `fdd`, ADC 40 MS/s
(`rx_path_rates` BBPLL 1280 MHz, ADC 40 MHz, RF 5 MHz). Load average 0.6–1.9 during all runs.

## Decision

**FDD on 2420/2460 MHz** (node 1 TX 2420 / RX 2460, node 2 the reverse), **RX gain 15 dB,
RX analog bandwidth 5 MHz** (design §1.3 step 1: PASS on both DUTs at S = 40 MHz, g = 15,
b = 5). `configs/profiles/duplex_fdd_2420_2460.yaml` is the profile for M2–M4. No LBT.

**But cap the TX duty at 0.7** (`node_app --tx-duty-cap`, default 0.7): USB 2.0, not RF, is the
limit of a duplex Pluto (M0 below).

## M0: USB (`../usb/`, `../usb_duty/`, `usb_capacity.py`)

Per Pluto, RX streaming at 5 MS/s on the shared context while its own TX pushes 6.9-ms noise
bursts at −89.75 dB (no RF of note):

| TX duty asked | …6e77 overflows/min | …6e77 TX duty reached | …f9c6 overflows/min | …f9c6 TX duty reached |
|---:|---:|---:|---:|---:|
| 0 (sink open, idle) | 0 | — | 0 | — |
| 0.5 | 0 | 0.50 | 0 | 0.50 |
| 0.6 | 0 | 0.60 | 0 | 0.60 |
| 0.7 | 0 | 0.70 | 0 | 0.70 |
| 0.8 | 222 | 0.78 | 0 | 0.79 |
| 0.9 | 224 | 0.78 | 0 | 0.79 |
| 0.9, both Plutos at once, 4 kernel buffers | 183 | 0.78 | 0 | 0.77 |
| 0.9, both Plutos at once, 8 kernel buffers | 177 | 0.78 | 0 | 0.77 |

60 s per row (30 s for 0.6–0.8); 0 start-up overflows in the first 3 s everywhere.

* Neither Pluto pushes more than ~0.78–0.79 TX duty while it receives 5 MS/s: RX 20 MB/s plus
  TX ~16 MB/s is the ceiling of one Pluto's USB 2.0 link.
* At that ceiling `…6e77` loses RX samples (an overflow in every 0.25-s poll, RX rate 4.78 MS/s);
  `…f9c6` does not. Same firmware and settings; the difference is the device or its port
  (root port vs. behind a hub). 8 kernel buffers do not help: this is throughput, not latency.
* Duty ≤ 0.7 is clean on both, alone and together, so the node caps its TX duty at 0.7.
* A second `IioContext` on a Pluto whose first context streams fails with EBUSY
  (`--probe-double-open`, same as the commander's probe): a node must share one context
  (`pluto.SharedPlutoContext`).
* Both Plutos receiving at 5 MS/s at once (commander's probe, 30 s): 0 overflows.

## M1: self-interference (`run1/`, `si_scan.py`)

`run1/si_summary.md` has every combination; `run1/si_scan.json` every phase record.
Grid: S = 20/40/70 MHz × both DUTs × RX gain 15/30/45 dB at b = 5 MHz, plus b = 4/8 MHz at
S = 40, g = 15, plus S = 0. Own TX: real v5 MCS 14 aggregates of 6.85 ms. **Own duty 0.7, not
the design's 0.9** (M0: 0.9 is not reachable and overflows one Pluto). Peer: MCS 8, one 900-B
datagram, 100 bursts/s, calibrated to ~15 dB ("mid") and ~10 dB ("edge") preamble SNR with
the own TX closed. 2 s per phase, 20 min in all.

At the operating point (own TX −5 dB, duty 0.7), S = 40 MHz, g = 15 dB:

| b MHz | DUT | ΔN dB | ΔN at 0 dB TX (unmasked) | ΔSNR dB | ΔSNR at 0 dB TX | loss c-edge → d-edge | clip | ΔOvf/min | verdict |
|---:|---|---:|---:|---:|---:|---|---:|---:|---|
| 5 | …6e77 (RX 2460) | 0.00 | −0.36 | 0.43 | 0.13 | 4.0 % → 2.5 % | 0 | 0 | PASS |
| 5 | …f9c6 (RX 2420) | 0.05 | −0.02 | 0.04 | 0.16 | 0 % → 0 % | 0 | 0 | PASS |
| 4 | …6e77 | 0.14 | −0.28 | 0.31 | 0.20 | ΔLoss −1.5 pp | 0 | 0 | PASS |
| 4 | …f9c6 | −0.07 | −0.07 | −0.11 | −0.10 | 0 → 0 | 0 | 0 | PASS |
| 8 | …6e77 | −0.04 | 0.03 | 0.03 | −0.01 | ΔLoss +2.5 pp | 0 | 0 | FAIL (ΔLoss) |
| 8 | …f9c6 | 0.00 | 0.01 | 0.06 | −0.05 | 0 → 0 | 0 | 0 | PASS |

ΔN = noise floor on the 52 data carriers with own bursts vs. own TX closed; ΔSNR = drop of the
peer's median preamble SNR; ΔLoss in percentage points of 200 peer bursts.

What the data say:

* **No measurable self-interference at any spacing at g = 15 and 30 dB.** ΔN is within
  ±0.15 dB at 20, 40 and 70 MHz, does not grow with the own TX gain (slope of the floor vs.
  own gain −0.04…+0.03 dB/dB; TX broadband noise would give ~+1), and is the same for the idle
  TX (b0), duty 0.3 (b5) and duty 0.7. ΔSNR ≤ 0.6 dB, mostly ≤ 0.2 dB, and has either sign. No
  spur from the own TX: the largest guard-bin line does not change between phases. The ADC
  runs at 40 MS/s, so the own TX 40 MHz away would alias onto DC; the analog filter keeps it
  below the floor (P_dc unchanged).
* **The "FAIL"s are not SI.** Every FAIL comes from ΔLoss, and ΔLoss is counting noise plus
  Wi-Fi: 1 burst is 0.5 pp of 200, and the edge phases run at 10 dB SNR. At 2460 MHz (Wi-Fi
  channel 11) the loss without own TX was already 4–5.5 % in some captures; at 2430 MHz an
  ambient signal sits 14–28 dB above the floor (phase a P_data −38.8 dBFS at g = 15). The same
  combinations have ΔN ≈ 0 and ΔSNR ≈ 0. The rule's 0.5-pp loss criterion needs ≥ 2000 bursts
  per phase to mean anything; M2–M4 measure loss over 60-s runs.
* **g = 45 dB clips.** With the peer at mid level the clip fraction reaches 1e-4 (…6e77) and
  3.9e-2 (…f9c6 at 2430 with the ambient signal); g = 45 is MARGINAL/FAIL from clipping, not
  from the own TX. g = 15 gives the peer 14–15 dB SNR at peer TX gain −8 dB (A←B) / −14 dB
  (B←A) and keeps 15–20 dB of headroom; the path is ~6 dB asymmetric (B hears A better).
* **ΔOvf = 0 everywhere** at duty 0.7.
* **Phase-a occupancy** (1-ms blocks > floor + 6 dB, g = 15/30/45): 2420 MHz 0 / 2.6 / 20 %,
  2460 MHz 1.6 / 7.8 / 29 %, 2405 0 / 2.6 / 10 %, 2475 0 / 0 / 0.05 %, 2440 5.7–7.7 %.
  2420 and 2460 lie in Wi-Fi channels 1 and 11; the LA will see their bursts as channel loss.
* **S = 0 (both 2440 MHz, own TX −20 dB, g = 15)**, for an LBT fallback that is not needed:
  own burst at −35.4 / −36.0 dBFS (no clipping, max 129 / 178 LSB of 2048), floor −50.9 /
  −53.1 dBFS; after a burst end the floor (+3 dB) returns within 46 / 14 samples median,
  845 / 224 samples p90 (0.17 / 0.05 ms). The burst detector split many bursts into pieces
  (OFDM envelope), so read these as an upper bound on the recovery, not a burst count.

Method notes: the phase-a and phase-b floors are unmasked and swing ±1 dB with Wi-Fi from one
2-s capture to the next, so `summarize` takes ΔN from the burst-masked floors with the peer on
(d-mid at −5 dB minus c-mid) for this run; later runs also record a masked "quiet" floor for
every phase. `run1/records.jsonl` keeps the per-bin spectrum of every phase.

Reproduce:

```bash
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONPATH=src
ACK='I acknowledge that this process will transmit RF'
python experiments/duplex_fdd/usb_capacity.py run --out experiments/duplex_fdd/results/usb \
    --probe-double-open --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/usb_capacity.py run --out experiments/duplex_fdd/results/usb_duty \
    --parts per-pluto --duties 0.6,0.7,0.8 --seconds 30 --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/si_scan.py run --dut 104473541196000419000c00f2ff1e6e77 \
    --peer 10447354119600041b000b00f2ff1ef9c6 --out experiments/duplex_fdd/results/si/run1 \
    --own-duty 0.7 --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/si_scan.py summarize experiments/duplex_fdd/results/si/run1
```

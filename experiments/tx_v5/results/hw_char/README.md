# Pluto hardware characterization for the v5 burst redesign (H0, H2a)

Scripts: `experiments/tx_v5/h0_push_model.py`, `experiments/tx_v5/h2a_guards.py`.
Raw IQ (not in repo): `/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad/hw_char/`.
Setup: 2.45 GHz, 5 MS/s, RX gain 15, TX -10 dB, loadavg1 0.5-0.9 before each run.

## H0: PlutoSampleSink push time
PlutoSampleSink.send pushes exactly the array it is given (scale to peak 0.7); the 256+256 guards
come from MessageTransmitter, not the sink. Back-to-back push, 10 s per size.
Fit of mean push time: t = 0.413 ms + 0.1939 us/sample * N, R^2 = 0.9994 (residuals -0.29..+0.15 ms).
Caveat: b ~ 1/fs (0.2 us); for N >= 20k the push is simply real-time bound (5.00 MS/s sustained), so
the linear fit overstates t_push above ~20k (actual = max(a+bN, N/fs)); the link is airtime-limited there.
See `h0_push_model.json` for the table and predicted max burst rates (duty cycle ~0.86-0.99 for >= 10k).

## H2a: guards
Burst = 160-sample ZC + 40 OFDM-like symbols x 80 = 3360 samples, 1000 bursts at 100/s per (pre, post),
plus 300 unpaced "b2b" pushes for 3 configs. Analysis: correlation detection, per-burst CFO (~-2 kHz),
17-tap FIR fit on the burst middle (samples 100..N-100), residual studied at the ends.
Burst-start detections: 1000 expected; 1000/1001/1000/1001/1000 (the two 1001 are one duplicate/spurious
peak, no missed bursts); no RX overflows.

Findings (all configs, incl. the 256/256 control, look the same):
- (a) Leading transient: error power per sample has a ~3 dB-peak-to-peak pattern locked to the ZC preamble
  (signal-dependent, present with 256 zeros of guard too), so "within 1 dB of median" is never reached in 64
  samples for ANY config. Differences vs the (256,256) control are max 0.4-0.7 dB (rms 0.15-0.24 dB) over the first
  64 samples, i.e. noise level (1000-burst average, ~0.15 dB std). No guard-dependent leading transient, even with pre=0.
  Pre-burst samples (-8..-1) are at noise floor (+-0.5 dB) for all configs.
- (b) Trailing: power is +5.3..5.6 dB above floor at the first sample after the nominal end (channel/filter
  delay, identical for all configs), 0.0..+1 dB from sample 2 on; 1-2 samples to within 1 dB of floor. Residual
  (beyond fitted channel) tail is at floor from sample 0. Mean of samples 64-256 is within +-0.15 dB of floor.
- (c) No hold/DC after the buffer ends: post-end window mean (0.06-0.07 +0.06-0.07j LSB) equals the idle DC
  (0.06+0.06j, LO leakage), noise SE 0.012. Short post-guards (0, 32) show nothing different. Unpaced pushes
  could not be made glued: for a 3360-sample burst the host needs ~1.2 ms/push vs 0.67 ms airtime, so
  consecutive bursts are ~2400 samples apart with idle in between (no overlap/glue observed; the push time
  model from H0 says glue is possible only if pushes outrun airtime, which they never do).
- (d) EVM (noise limited, -17.5 dB everywhere): last 80 samples are +1.35..1.56 dB above the median window, first 80 are
  +2.8..3.1 dB (mostly the preamble/fit-edge effect + slow drift), but the same in the 256/256 control: delta vs
  control in [-0.17, +0.12] dB.
- (e) detections as above.

Recommendation: (0, 0) shows no measurable penalty vs (256, 256) in leading error, trailing ring, DC or last-80 EVM
(all deltas <= ~0.4 dB, within measurement noise); (0, 32) and (16, 64) likewise. Smallest measured: pre=0, post=0.
A conservative choice is (0, 32) (1-2 samples of channel ring after the end are absorbed by 32 zeros).
Caveat: with pre=0 the RX sees the unmodelled turn-on only through the average over bursts at this SNR (~17 dB);
the low-SNR loss with the real v5 decoder (timing/AGC/channel-estimate sensitivity to the burst edges) must be
checked later. Analysis uses 10 ms-spaced bursts, so it does not exercise TX buffer underrun glue at higher duty.

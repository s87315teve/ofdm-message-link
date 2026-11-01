# M4: video in both directions at once, link adaptation on

Same bench as M2/M3, git `6ba3868` (OLLA default, start-to-start burst interval, RX fix). Both
nodes at TX −5 dB, RX gain 15 dB, `--la olla` from MCS 8, 3 decode workers each, TX duty cap
0.7. Video: A→B from the webcam (`/dev/video0`, MJPEG 1280×720 at 15 fps), B→A from
`ffmpeg -f lavfi testsrc2`, both H.264 CBR in MPEG-TS, 752-B UDP packets, 60 s, with the encoder
settings of `scripts/run_ota_video_demo.sh`. Each consumer is `ffmpeg -i udp://… -f null -`,
whose `corrupt` and `error` lines are counted. M3 showed MCS ≥ 10 at −5 dB in both directions, so
after 750 kbit/s the run was repeated at 1300 kbit/s as planned.

```bash
bash experiments/duplex_fdd/video_duplex.sh --enable-rf --out experiments/duplex_fdd/results/m4/video_750k \
    --kbps 750 --seconds 60 --la olla --tx-gain -5
bash experiments/duplex_fdd/video_duplex.sh --enable-rf --out experiments/duplex_fdd/results/m4/video_1300k \
    --kbps 1300 --seconds 60 --la olla --tx-gain -5
PYTHONPATH=src python experiments/duplex_fdd/summarize_duplex.py experiments/duplex_fdd/results/m4/video_*
```

## Results

Frames = frames the consumer decoded (60 s × 14.7 fps from the camera, 15 fps from lavfi). Corrupt
and error = lines in `video_*_rx.log`. Loss = receiver subframe (datagram) loss over the run. MCS
and SNR while the video ran. CPU in cores, from the stats lines, while the video ran.

| kbit/s | dir | frames | corrupt / error lines | datagram loss | MCS (OLLA) | SNR dB | sender airtime | RX ovf | engine CPU tx / rx | workers rx |
|---:|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 750 | A→B (camera) | 883 | 2 / 2 | 0.01 % | 17 after 0.1 s, 3 changes | 23.3 | ≤ 0.09 | 1 (node 2) | 0.69 / 0.77 | 0.26 |
| 750 | B→A (testsrc2) | 877 | 96 / 65 | 1.15 % | ~10 (8–15), 92 changes | 17.0 | ≤ 0.35 | 0 (node 1) | 0.77 / 0.69 | 0.28 |
| 1300 | A→B (camera) | 884 | 4 / 2 | 0.02 % | 17, 3 changes | 23.1 | ≤ 0.15 | 0 | 0.66 / 0.82 | 0.39 |
| 1300 | B→A (testsrc2) | 884 | 155 / 125 | 1.15 % | ~10 (8–15), 103 changes | 16.8 | ≤ 0.54 | 1 | 0.82 / 0.66 | 0.42 |

Feedback flowed both ways throughout (never stale). Neither node heard its own bursts. No
datagram was dropped at a sender (`tx.ingress_dropped_queue_full` = 0).

## What the data say

* **Both directions carry live video at once.** The consumers decoded 877–884 frames, the full
  60 s, in both directions at 750 and at 1300 kbit/s. The corrupt lines count damaged frames
  among them.
* **A→B (camera, 2420 MHz) is clean.** 2–4 corrupt lines in 60 s, 0.01–0.02 % datagram loss. OLLA
  moves from MCS 8 to 17 within 0.1 s of the first feedback and holds it, so the camera stream
  needs only 9–15 % airtime.
* **B→A (2460 MHz) shows the loss floor of that channel.** 96 (750 kbit/s) and 155 (1300 kbit/s)
  corrupt lines come from 1.15 % datagram loss at 17 dB SNR, 6 dB above MCS 10's threshold. This is
  the same ~0.8–1.1 % floor M2 and M3 measured on this Wi-Fi channel-11 path. OLLA keeps the loss
  close to its 1 % target by holding a negative offset (−3 to −4.6 dB) at about MCS 10, still changing MCS
  about 1.5 times a second. Loss of this kind would need FEC or ARQ above the PHY (design
  section 8), or a quieter channel.
* **Load:** the engines used 0.66–0.82 core, and the decode workers 0.26–0.42 core per node. RX
  overflows: 0–1 per run.

## Script fix found by this run

At the end of the first run (750 kbit/s) the two consumer `ffmpeg`s did not exit on SIGTERM. A
process blocked in a UDP read does not act on the signal until a packet arrives, so
`video_duplex.sh` waited until I killed them by hand, after ~8 min. The counts were not affected:
the consumers had already logged everything, and the nodes stopped at their `--duration`. The
consumer URL now has `&timeout=5000000` (5 s without data ends ffmpeg), and SIGKILL follows if a
consumer is still alive 8 s after SIGTERM. The 1300 kbit/s run used the fixed script and ended by
itself.

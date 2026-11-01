# M1 self-interference scan: decision table

Decision (design §1.3, step 1): `{"step": 1, "mode": "fdd", "frequencies_mhz": [2420, 2460], "rx_gain_db": 15, "rx_bw_hz": 5000000.0, "overflow_fails_everywhere": false}`

Criteria at own TX -5 dB, the run's own duty (see duty b3), both DUTs: PASS ΔN ≤ 1.0 dB, ΔSNR ≤ 1.0 dB, ΔLoss ≤ 0.5 pp, clip < 1e-6, ΔOvf ≤ 0.5/min; MARGINAL ΔSNR ≤ 3.0 dB, ΔLoss ≤ 2 pp, clip < 1e-4.

| S MHz | g dB | b MHz | verdict | per DUT | baseline SNR min | ΔSNR max |
|---:|---:|---:|---|---|---:|---:|
| 20 | 15 | 5 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 14.54 | 0.18 |
| 20 | 30 | 5 | FAIL | {'1e6e77': 'PASS', '1ef9c6': 'FAIL'} | 27.51 | 0.56 |
| 20 | 45 | 5 | FAIL | {'1e6e77': 'MARGINAL', '1ef9c6': 'FAIL'} | 32.93 | 1.78 |
| 40 | 15 | 4 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 13.98 | 0.31 |
| 40 | 15 | 5 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 14.91 | 0.43 |
| 40 | 15 | 8 | FAIL | {'1e6e77': 'FAIL', '1ef9c6': 'PASS'} | 14.4 | 0.06 |
| 40 | 30 | 5 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 27.15 | -0.12 |
| 40 | 45 | 5 | MARGINAL | {'1e6e77': 'MARGINAL', '1ef9c6': 'PASS'} | 32.49 | 0.75 |
| 70 | 15 | 5 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 14.59 | 0.47 |
| 70 | 30 | 5 | PASS | {'1e6e77': 'PASS', '1ef9c6': 'PASS'} | 27.19 | -0.47 |
| 70 | 45 | 5 | MARGINAL | {'1e6e77': 'MARGINAL', '1ef9c6': 'PASS'} | 31.03 | 2.66 |

## Per combination

| combo | duty b3 | ΔN | ΔN/burst | ΔN idle | ΔN 0 dB | ΔN D0.3 | slope | ΔSNR | ΔSNR 0 dB | ΔLoss pp | clip | ΔOvf/min | base SNR | occ. a | spur guard | verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| S20_1e6e77_g15_b5 | 0.7 | -0.07 | -0.1 | 0.0 | -0.04 | 0.01 | -0.0 | 0.11 | 0.01 | 0.0 | 0.0 | 0.0 | 14.54 | 0.0 | 1.01 | PASS |
| S20_1e6e77_g30_b5 | 0.7 | -0.01 | -0.01 | -0.14 | -0.32 | -0.19 | -0.011 | 0.56 | 0.96 | 0.0 | 0.0 | 0.0 | 27.51 | 0.055401662049861494 | 0.5 | PASS |
| S20_1e6e77_g45_b5 | 0.7 | -0.42 | -0.61 | -1.08 | 0.15 | -0.73 | 0.019 | 1.38 | 2.59 | 0.0 | 0.0 | 0.0 | 32.93 | 0.07803065490013934 | 2.91 | MARGINAL |
| S20_1ef9c6_g15_b5 | 0.7 | -0.01 | -0.01 | 0.17 | 0.41 | 1.25 | 0.03 | 0.18 | 0.1 | -4.5 | 0.0 | 0.0 | 15.34 | 0.09382257315373897 | 1.32 | PASS |
| S20_1ef9c6_g30_b5 | 0.701 | 0.02 | 0.03 | 2.37 | 0.6 | 0.72 | -0.024 | 0.09 | 0.66 | 5.0 | 0.0 | 0.0 | 28.6 | 0.08221086855550395 | 2.19 | FAIL |
| S20_1ef9c6_g45_b5 | 0.701 | 0.21 | 0.3 | -0.48 | -0.31 | -0.49 | 0.03 | 1.78 | 3.53 | -7.5 | 0.03905226544636052 | 0.0 | 34.81 | 0.2289828146771946 | 1.69 | FAIL |
| S40_1e6e77_g15_b4 | 0.7 | 0.14 | 0.2 | -0.12 | -0.28 | 0.13 | -0.004 | 0.31 | 0.2 | -1.5 | 0.0 | 0.0 | 13.98 | 0.02816251154201293 | 0.33 | PASS |
| S40_1e6e77_g15_b5 | 0.7 | 0.0 | 0.0 | 0.03 | -0.36 | -0.34 | -0.025 | 0.43 | 0.13 | -1.5 | 0.0 | 0.0 | 14.91 | 0.016256386437529028 | 0.54 | PASS |
| S40_1e6e77_g15_b8 | 0.701 | -0.04 | -0.06 | -0.03 | 0.03 | -0.12 | 0.003 | 0.03 | -0.01 | 2.5 | 0.0 | 0.0 | 14.4 | 0.02786809103576405 | 0.22 | FAIL |
| S40_1e6e77_g30_b5 | 0.7 | -0.03 | -0.04 | 0.12 | 0.37 | 0.08 | -0.002 | -0.25 | -0.02 | -2.5 | 0.0 | 0.0 | 27.15 | 0.07849512308406874 | 0.42 | PASS |
| S40_1e6e77_g45_b5 | 0.7 | 0.51 | 0.71 | 1.05 | 1.56 | 2.18 | 0.02 | 0.75 | 2.66 | 1.5 | 8.308596727324695e-05 | 0.0 | 32.49 | 0.28889921040408734 | 4.63 | MARGINAL |
| S40_1ef9c6_g15_b4 | 0.7 | -0.07 | -0.1 | -0.01 | -0.07 | -0.07 | -0.005 | -0.11 | -0.1 | 0.0 | 0.0 | 0.0 | 14.18 | 0.0 | 0.74 | PASS |
| S40_1ef9c6_g15_b5 | 0.7 | 0.05 | 0.07 | -0.02 | -0.02 | 0.01 | -0.001 | 0.04 | 0.16 | 0.0 | 0.0 | 0.0 | 15.23 | 0.0 | 0.53 | PASS |
| S40_1ef9c6_g15_b8 | 0.701 | 0.0 | 0.0 | 0.01 | 0.01 | 0.0 | -0.0 | 0.06 | -0.05 | 0.0 | 0.0 | 0.0 | 14.84 | 0.0 | 2.01 | PASS |
| S40_1ef9c6_g30_b5 | 0.701 | 0.01 | 0.01 | -0.39 | -0.31 | -0.28 | 0.009 | -0.12 | 0.43 | 0.0 | 0.0 | 0.0 | 28.04 | 0.026474686483975846 | 3.01 | PASS |
| S40_1ef9c6_g45_b5 | 0.7 | -0.04 | -0.06 | -0.47 | -0.41 | -0.95 | 0.005 | -0.55 | 2.43 | 0.0 | 0.0 | 0.0 | 33.03 | 0.19693450998606596 | 2.27 | PASS |
| S70_1e6e77_g15_b5 | 0.698 | 0.03 | 0.04 | 0.0 | 0.03 | 0.04 | 0.002 | 0.47 | 0.04 | 0.0 | 0.0 | 0.0 | 14.59 | 0.0 | 0.68 | PASS |
| S70_1e6e77_g30_b5 | 0.701 | 0.01 | 0.01 | 0.08 | 0.04 | 0.05 | -0.001 | -0.6 | 0.75 | 0.0 | 0.0 | 0.0 | 27.19 | 0.0 | 1.82 | PASS |
| S70_1e6e77_g45_b5 | 0.701 | 0.16 | 0.23 | 0.2 | 0.03 | 0.16 | 0.003 | 2.66 | -0.35 | 0.0 | 0.0 | 0.0 | 31.03 | 0.00046446818392940084 | 8.21 | MARGINAL |
| S70_1ef9c6_g15_b5 | 0.701 | 0.01 | 0.01 | -0.04 | -0.02 | 0.01 | -0.001 | 0.09 | 0.04 | 0.0 | 0.0 | 0.0 | 15.16 | 0.0 | 1.23 | PASS |
| S70_1ef9c6_g30_b5 | 0.7 | 0.03 | 0.04 | -0.22 | -0.2 | -0.14 | 0.007 | -0.47 | 0.19 | 0.0 | 0.0 | 0.0 | 27.76 | 0.02631578947368421 | 3.42 | PASS |
| S70_1ef9c6_g45_b5 | 0.7 | -1.46 | -2.28 | -0.16 | -0.43 | -0.71 | -0.037 | 0.13 | 4.0 | 0.0 | 0.0 | 0.0 | 33.95 | 0.09800278680910357 | 2.3 | PASS |

## Phase-a occupancy per frequency (1-ms blocks > floor + 6 dB)

* 2405 MHz: [0.0, 0.02631578947368421, 0.09800278680910357], P_data [-55.97, -53.62, -41.42] dBFS
* 2420 MHz: [0.0, 0.026474686483975846, 0.19693450998606596, 0.0, 0.0], P_data [-56.02, -54.41, -43.55, -54.96, -55.69] dBFS
* 2430 MHz: [0.09382257315373897, 0.08221086855550395, 0.2289828146771946], P_data [-38.83, -24.95, -11.07] dBFS
* 2440 MHz: [0.0566651184393869, 0.07710171853228054], P_data [-52.31, -52.67] dBFS
* 2450 MHz: [0.0, 0.055401662049861494, 0.07803065490013934], P_data [-52.69, -51.1, -39.68] dBFS
* 2460 MHz: [0.016256386437529028, 0.07849512308406874, 0.28889921040408734, 0.02816251154201293, 0.02786809103576405], P_data [-51.91, -44.44, -29.65, -51.42, -51.59] dBFS
* 2475 MHz: [0.0, 0.0, 0.00046446818392940084], P_data [-52.82, -48.26, -34.19] dBFS

## S = 0 (both 2440 MHz, own TX -20 dB)

* {"dut": "1e6e77", "bursts": 20083, "burst_level_dbfs": -35.36, "floor_dbfs": -50.86, "smoothing_samples": 32, "recovery_samples_median": 46.0, "recovery_samples_p90": 845.0, "recovery_samples_max": 6577, "recovery_ms_p90": 0.169, "clip_fraction": 0.0, "max_abs_lsb": 129}
* {"dut": "1ef9c6", "bursts": 234, "burst_level_dbfs": -35.99, "floor_dbfs": -53.09, "smoothing_samples": 32, "recovery_samples_median": 14.0, "recovery_samples_p90": 223.8, "recovery_samples_max": 20952, "recovery_ms_p90": 0.045, "clip_fraction": 0.0, "max_abs_lsb": 178}

Run: git 7292870135, load at start [0.8, 0.92, 1.39], max load1 1.9, invalid load False.

```
/:  Bus 04.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/9p, 20000M/x2
    |__ Port 9: Dev 2, If 0, Class=Hub, Driver=hub/4p, 5000M
        |__ Port 1: Dev 3, If 0, Class=Hub, Driver=hub/4p, 5000M
/:  Bus 03.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/14p, 480M
    |__ Port 2: Dev 2, If 0, Class=Vendor Specific Class, Driver=, 12M
    |__ Port 2: Dev 2, If 2, Class=Human Interface Device, Driver=usbhid, 12M
    |__ Port 3: Dev 12, If 0, Class=Communications, Driver=rndis_host, 480M
    |__ Port 3: Dev 12, If 5, Class=Communications, Driver=, 480M
    |__ Port 3: Dev 12, If 3, Class=Communications, Driver=cdc_acm, 480M
    |__ Port 3: Dev 12, If 1, Class=CDC Data, Driver=rndis_host, 480M
    |__ Port 3: Dev 12, If 4, Class=CDC Data, Driver=cdc_acm, 480M
    |__ Port 3: Dev 12, If 2, Class=Mass Storage, Driver=usb-storage, 480M
    |__ Port 5: Dev 3, If 2, Class=Audio, Driver=snd-usb-audio, 480M
    |__ Port 5: Dev 3, If 0, Class=Video, Driver=uvcvideo, 480M
    |__ Port 5: Dev 3, If 3, Class=Audio, Driver=snd-usb-audio, 480M
    |__ Port 5: Dev 3, If 1, Class=Video, Driver=uvcvideo, 480M
    |__ Port 10: Dev 4, If 0, Class=Hub, Driver=hub/4p, 480M
        |__ Port 1: Dev 6, If 0, Class=Hub, Driver=hub/4p, 480M
            |__ Port 3: Dev 8, If 3, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 1, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 2, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 0, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 5, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 3, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 1, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 4, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 2, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 0, Class=Human Interface Device, Driver=usbhid, 12M
        |__ Port 4: Dev 10, If 1, Class=CDC Data, Driver=rndis_host, 480M
        |__ Port 4: Dev 10, If 4, Class=CDC Data, Driver=cdc_acm, 480M
        |__ Port 4: Dev 10, If 2, Class=Mass Storage, Driver=usb-storage, 480M
        |__ Port 4: Dev 10, If 0, Class=Communications, Driver=rndis_host, 480M
        |__ Port 4: Dev 10, If 5, Class=Communications, Driver=, 480M
        |__ Port 4: Dev 10, If 3, Class=Communications, Driver=cdc_acm, 480M
        |__ Port 2: Dev 7, If 0, Class=Wireless, Driver=btusb, 12M
        |__ Port 2: Dev 7, If 1, Class=Wireless, Driver=btusb, 12M
    |__ Port 14: Dev 5, If 2, Class=Wireless, Driver=, 480M
    |__ Port 14: Dev 5, If 0, Class=Wireless, Driver=btusb, 480M
    |__ Port 14: Dev 5, If 1, Class=Wireless, Driver=btusb, 480M
/:  Bus 02.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/1p, 20000M/x2
/:  Bus 01.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/1p, 480M
```

# 強化接收端：做法、模擬與實機 A/B

[SISO 對照報告](../siso_comparison/README.md)指出這個專案的 RX 有兩個問題：Turbo decoder 拿到的
LLR 全部飽和，以及 header 太脆弱。這個資料夾記錄 `improve-rx` 分支怎麼修、修了多少，
以及量測用的所有腳本與數據。

**只改接收端，wire format 不變。** v1.0.0 的 TX 送出的 burst，新 RX 照樣收；新舊 RX 可以拿
同一份錄音直接比。要改 TX 的想法另外寫在 [`tx-proposal.md`](tx-proposal.md)，這個分支沒有實作；
後來由 `tx-v5` 分支實作成 wire v5，見 [`../tx_v5/`](../tx_v5/README.md)。

## 1. 結論

兩台 Pluto、2.45 GHz、900-byte burst，**同一份錄音**用新舊兩版 RX 各解一次：

| | MCS 0 遺失 < 1% 的最低 TX gain | MCS 4 遺失 < 1% 的最低 TX gain |
|---|---:|---:|
| v1.0.0 | −10 dB | −7.5 dB |
| 新 RX | **−20 dB** | **−12.5 dB** |

同樣不掉包，MCS 0 可以少發約 10 dB、MCS 4 約 5 dB（量測間距 2.5 dB）。

即時跑真的 `tx_app`／`rx_app`（每秒 100 個 datagram、30 秒、新舊輪流各兩次）：

| | v1.0.0 RX | 新 RX |
|---|---:|---:|
| MCS 0，TX −20 dB | 76–77% 遺失 | **0.8–1.3%** |
| MCS 4，TX −15 dB | 48–49% 遺失 | **1.2–1.5%** |

完整的表與條件見 [docs/08 8.7](../../docs/08-measurements.md#87-改進-rx-之後pluto--pluto245-ghz)。

模擬（900 B frame，AWGN 或固定的 5-tap multipath，10 kHz CFO，每點 150 個 burst）：

| | 50% 成功 v1.0.0 | 50% 成功 新 RX | 99% 成功 v1.0.0 | 99% 成功 新 RX |
|---|---:|---:|---:|---:|
| QPSK，AWGN | 7.5 dB | 2.6 dB | 11.6 dB | 4.5 dB |
| QPSK，multipath | 10.4 dB | 4.2 dB | 12 dB 以上 | 6.2 dB |
| 16QAM，AWGN | 12.9 dB | 6.4 dB | 16.2 dB | 8.5 dB |
| 16QAM，multipath | 15.1 dB | 8.0 dB | 18 dB 以上 | 10.7 dB |

模擬沒有相位雜訊、IQ imbalance 與 TX 失真；實機在同樣的 SNR 下難解 3–4 dB（見第 2 節第 5 項）。

## 2. 改了什麼

| # | 問題 | 做法 | 程式 |
|---|---|---|---|
| 1 | `link.py` 沒有把 noise variance 傳給 decoder，`burst.py` 用 `1e-6`，LLR 全部被截在 ±64：Turbo decoder 拿到的等於 hard decision | 每個 burst 自己量 LLR 用的雜訊：header 解出來之後，它的 144 個 QPSK symbol 就是已知的，等化後的誤差就是 payload 會遇到的雜訊（熱雜訊、channel 估計誤差、相位雜訊、IQ imbalance、TX 失真都在內） | `burst.py` `_header_noise_variance` |
| 2 | Channel estimate 只來自一個 training symbol，每個 carrier 各自 LS，等化後 noise 約放大 3 dB | Impulse response 比 cyclic prefix 短，所以把 52 個 LS 值投影到 20 個 tap（−4 … 15）上，估計誤差少約 4 dB。殘差比 preamble 的雜訊大 3 倍以上時（timing 錯、delay 太長）退回原本的 LS | `channel.py` `refine_channel_estimate` |
| 3 | Header 要 96 bit 全對 CRC 才過，3 次重複只有約 4.8 dB 增益 | CRC 失敗、soft 翻轉也失敗之後，從**所有合法的 header**（約 8 200 個：4 種 wire version × 2 種 MCS × frame 長度）中挑與收到的 soft 值最相關的。分數低於 0.75 就拒絕（純雜訊最高 0.68） | `burst_header.py` `decode_burst_header_list`；`burst.py` `_header_candidates` |
| 4 | 修完 1–3 之後，acquisition 變成最先失效的地方：0.40 的門檻擋掉一半解得出來的 burst | `detection_threshold` 0.40 → 0.25、`correlation_threshold` 0.40 → 0.30。空中雜訊的 correlation 最高 0.203 | `sync.py`、`config.py`、`configs/profiles/ota_2p45ghz.yaml` |
| 5 | 畫面上的 SNR 由 EVM 推算，只算解出來的 burst，比真實 SNR 低 3–5 dB | Preamble 兩個重複的半段：相同的部分是訊號、不同的部分是雜訊。兩半各做 32 點 FFT，**只用落在資料 carrier 上的 26 個 bin**；沒解出來的 burst 也算 | `sync.py` `measure_preamble`；`streaming.py` `link_snr_db` |

第 5 項為什麼只看資料 carrier：Pluto 的 guard band 雜訊比資料 carrier 高約 10 dB（`noise_spectrum.py`），
用整個頻寬估會把 SNR 讀低約 4 dB。模擬是白雜訊，在 0–20 dB 讀數與真實 SNR 差 ±0.62 dB 內
（`snr_check.py`）。

第 1 項為什麼不用熱雜訊：Turbo decoder 內部也截在 ±64，LLR 太有自信會吃虧。用熱雜訊算 LLR 時，
同一份錄音的 MCS 0 在 TX −20 dB 遺失 1.25%；改用 header 量到的雜訊（中位數比熱雜訊高 3.3–3.7 dB）
是 0.25%。

GUI 的 MCS 門檻用新的 SNR 重訂：MCS 0 = 9.0 dB、MCS 4 = 15.0 dB。比 v1.0.0 的 8／11 dB 高，
是因為刻度換了（舊的 EVM 推算值比熱雜訊 SNR 低 2–4 dB），不是 RX 變差。

## 3. 怎麼量的

> 這一節的指令要在 improve-rx 的 tree 上跑（`git worktree add <dir> improve-rx`），因為目前的 `tx_app` 只送 wire v5，這些指令用的舊版 MCS 編號與 TX 行為在現在的 tree 上已不成立。

### 模擬

- `sim_sweep.py`：每點 150 個 burst，同樣的 seed，新舊 RX 各跑一次。SNR = burst 平均 sample 功率 ÷ noise variance。
- `snr_check.py`：SNR 讀數的偏差與跳動。
- `cpu_bench.py`：單核解一個 burst 的時間。

### 實機錄音 A/B

1. `ota_capture.py`：一台 Pluto 用 app 的 `MessageTransmitter` 送 400 個 burst（每秒 100 個），
   另一台錄原始 IQ（int16，每份約 100 MB，**沒有放進 repo**；每份的 `.json` 在 `results/captures/`）。
2. `offline_decode.py`：把錄音照 `rx_app` 的方式（Pluto 大小的 chunk、`MessageReceiver`）餵進去，
   數有幾個 sequence 解出來。用 `PYTHONPATH` 選哪一版 RX：

```bash
git worktree add /tmp/v1 v1.0.0          # 再把 _turbo_native*.so 複製到 /tmp/v1/src/ofdm_link/phy/
PYTHONPATH=src python experiments/rx_improvement/ota_capture.py --out caps --mcs qpsk \
    --tx-gains -25 -22.5 -20 -17.5 -15 -12.5 -10 -7.5 -5 -2.5 0 \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
PYTHONPATH=src python experiments/rx_improvement/offline_decode.py caps new.json
(cd /tmp/v1 && PYTHONPATH=src python /path/to/offline_decode.py /path/to/caps old.json)
```

**v1.0.0 必須讀它自己的 `configs/`**（在 `/tmp/v1` 底下執行），否則會拿到新的 acquisition 門檻。

3. `noise_scan.py`：TX 關閉，只收，記錄空中雜訊能讓 acquisition 的分數到多高。這台電腦跟不上即時，
   120 秒只處理了約 45 秒的 samples，所以兩段加起來約 90 秒。
4. `noise_spectrum.py`：錄音開頭沒有 burst 的那段，雜訊在資料 carrier 與 guard band 各多少。

### 即時 run

- `live_run.sh RX_SRC TX_GAIN MCS RATE SECONDS OUT`：啟動真的 `rx_app`（從 `RX_SRC` import，讀它旁邊的
  `configs/`）、`tx_app`，用 `../ota_udp_throughput/udp_load.py` 送 datagram 並逐 byte 比對。
- `summarize_live.py`：遺失率、送資料期間的 RX overflow（不含啟動時 decode pool 產生的那一兩次）、
  畫面 SNR 的平均與跳動。
- 影像用 `scripts/run_ota_video_demo.sh --transport pluto --auto-start --duration 60`，
  無螢幕時加 `QT_QPA_PLATFORM=offscreen SDL_VIDEODRIVER=dummy`。
- **量測時電腦不要跑其他重的工作。** 背景有模擬在跑時，新 RX 出現過 RX overflow 與 7–10% 遺失；
  閒置時重跑是 0 overflow、約 1%。

## 4. 檔案

| 檔案 | 內容 |
|---|---|
| `results/sim_v1.json`、`sim_new.json` | 模擬，新舊 RX |
| `results/ota_v1.json`、`ota_new.json` | 22 份錄音，新舊 RX（新 RX 是最終版） |
| `results/thresholds_*.json` | 邊緣那 9 份錄音在三組 acquisition 門檻下的結果（用當時的 RX 版本，SNR 欄位是舊的時域估計） |
| `results/noise_rx15.json`、`noise_rx30.json` | 空中雜訊掃描 |
| `results/noise_spectrum.txt` | 雜訊在資料 carrier 與 guard band 的比較 |
| `results/live.json` | 即時 A/B（新舊各兩次，電腦閒置） |
| `results/live_high_rate.json` | MCS 4、每秒送 380 個 |
| `results/video_tx*/` | 兩次影像 run 的 `rx_stats.jsonl` 與 ffplay `corrupt` 次數 |
| `results/snr_estimator_check.txt`、`cpu_bench.txt` | `snr_check.py`、`cpu_bench.py` 的輸出 |

## 5. 證據邊界

- 只有 Pluto 對 Pluto、一個擺放位置、一天。B210／N210／2901 配新 RX 沒有量。
- 錄音每個點 400 個 burst：遺失率 0.25% 就是 1 個 burst。
- MCS 4 在 TX −15 dB 與 −12.5 dB 之間沒有點，所以 15.0 dB 的門檻偏保守。
- Pluto 發送端在 MCS 4 約每秒 330 個 burst 就飽和，更高速率下 RX 的表現沒有測。
- 低 SNR 時每個 burst 要跑滿 Turbo iteration，decode worker 需要空的 CPU。

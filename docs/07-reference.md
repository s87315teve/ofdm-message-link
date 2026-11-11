# 7. 參考資料

## 7.1 命令列參數

`python -m ofdm_message_link.tx_app --help` 與 `rx_app --help` 會列出完整說明。

### 兩支程式共用

| 選項 | 說明 |
|---|---|
| `--config` / `--overlay` | 設定檔，預設 `configs/default.yaml` + `configs/profiles/ota_2p45ghz.yaml`；`--overlay` 可重複 |
| `--transport {udp,uhd,pluto}` | 預設 `udp`（模擬）；`uhd` 是 USRP，`pluto` 是 ADALM-Pluto |
| `--mcs {qpsk,qam16}` | TX 的簡寫：`qpsk` = MCS 8（QPSK 1/3）、`qam16` = MCS 12（16QAM 1/2）；同時給 `--mcs-index` 時後者優先。RX 僅作 display hint，不參與 decode |
| `--mcs-index N` | TX 啟動時的 wire-v5 MCS，8–17，**預設 8**（給 `--mcs qam16` 時是 12）。11 是選單隱藏的實驗用 MCS。舊格式的 0–7 TX 會拒絕；RX 要設 `phy.rx.legacy_acquisition: true`（v2.0 起預設關）才解舊版 TX 送的 0–7 burst。RX 接受 0–17，僅作 display hint |
| `--frame-payload-bytes` | PHY frame payload 大小，預設 996 |
| `--udp-host` / `--udp-port` | 模擬模式的 sample transport 位址，預設 `127.0.0.1:52101` |
| `--serial` | 用 serial 預選 USRP 或 Pluto |
| `--channel` | 預選 front end：B210／2901 的 0 = RF A、1 = RF B；N210 只有 0 |
| `--antenna` | 預選 antenna port，例如 `TX/RX` 或 `RX2`（N210 上是 RF1／RF2） |
| `--gain` | 預選 gain（dB）；沒給時 TX 預設為最低值。Pluto 的 TX gain 是 −89.75 到 0 dB 的 attenuator |
| `--auto-start` | Probe 完成後直接啟動 radio，不等 Start 按鈕；供腳本與可重現的執行 |
| `--ready-file PATH` | Radio 開始運作時建立 PATH，停止或關視窗時刪除，讓腳本可以等操作者在視窗裡選好裝置與 gain |
| `--engine {process,thread}` | 預設 `process`：radio、encode/decode 與 UDP ingress/egress 在子 process，GUI 不會卡住它們 |
| `--ui-fps` | Constellation 與 spectrum 的刷新率，1–60 Hz，預設 15 Hz |
| `--log-lines N` | `Log` tab 保留的行數，預設 2000 |
| `--geometry WxH+X+Y` | 視窗內容區大小與外框左上角位置 |
| `--cpu-affinity CPUS` | 把 app 與它啟動的每個 process（engine、decode workers）綁在 CPUS：`0-7`、`2,4-5` 這種列表，或 `p-cores`（讀 `/sys/devices/cpu_core/cpus`，沒有這個檔就不綁，記一行 log）。**預設不綁**。見 [7.10 節](#710-效能開關與-native-模組) |
| `--realtime[=PRIO]` | 只限 Linux：engine process 的 thread 用 SCHED_RR、優先權 PRIO（1–49，只寫 `--realtime` 時是 15），decode workers 維持一般排程，不動 RT throttling。可用 CPU 少於 4 個時拒絕，除非加 `--force`。**預設關閉** |
| `--force` | 允許在少於 4 個可用 CPU 的機器上用 `--realtime` |
| `--native-backend {auto,python,native}` | Acquisition、header probe、Turbo kernel、TX encode 用 native 還是 Python 參考實作；預設 `auto`（有 build 就用 native，沒有就退回 Python 並警告一次）。設定 `OFDM_LINK_NATIVE` |

### 只有發送端

| 選項 | 說明 |
|---|---|
| `--stats-log PATH` | 每次統計刷新附加一行 JSON：messages、bursts、goodput、airtime 與 `tx_health`（見下）。`t` 是 monotonic 時間，`wall_time` 是 Unix 時間（秒），用來和其他機器的紀錄對時。`goodput_bps`、`airtime` 是 2 秒視窗，流量停了就歸零；`goodput_avg_bps`、`airtime_avg` 是開始（或重設統計）以來的平均。`--stats-log-max-mb`／`--stats-log-backups` 與接收端相同 |
| `--ingress-port` | 接受 application bytes 的 UDP port，預設 52001 |
| `--peak-amplitude` | 每個 burst 發射前正規化到的 peak，預設 0.7 |
| `--aggregation {on,off}` | `on`：radio 可以接下一個 burst 時，把 queue 裡的 datagram 放進同一個 burst（在下面的上限內）。`off`：一個 burst 一個 datagram，供 A/B 量測。預設取 `mac.aggregation.enabled`（`on`） |
| `--aggregation-max-subframes N` | 一個 burst 最多幾個 datagram，1–64，預設取設定檔（16） |
| `--aggregation-max-wait-ms MS` | 還有空間的 burst 最多等 MS 毫秒（從最早的 message 到達算起），讓更多 datagram 一起走。0–1000，預設 0：不等，負載低時不增加延遲 |
| `--min-burst-interval-ms MS` | 兩個 burst 開始交給 radio 的時間至少隔多久（start 到 start：比間隔長的 burst 後面不會多等），0–50，預設取設定檔（4 ms，最多每秒 250 個 burst）；等的時候繼續收 datagram，讓下一個 burst 帶比較多。閒置之後的第一個 burst 不多等，滿的 burst 不會被留。0 關閉。實際等待是它與 `--aggregation-max-wait-ms` 較大的那個 |
| `--max-burst-airtime-ms MS` | 聚合後的 burst 最長多久（含 guard），大於 0、最大 100，預設取設定檔（8 ms）。不論設多少，聚合也會在 80 000 samples 停止；超過上限的單一 datagram 仍然單獨送出 |
| `--enable-rf` / `--acknowledgement` | RF 發射授權，`--transport uhd` 與 `--transport pluto` 必要。Acknowledgement 的文字是 `I acknowledge that this process will transmit RF`，必須一字不差 |

### 只有接收端

| 選項 | 說明 |
|---|---|
| `--egress-host` / `--egress-port` | 轉發已交付訊息的目的地，預設 `127.0.0.1:52002` |
| `--snr-db` | 只適用 `udp`，加上 AWGN。對真實 channel 無效，帶了會被拒絕 |
| `--decode-workers` | Payload decode 的 process 數，預設 4；1 = 單 thread inline decoder |
| `--rx-recv-frames` | UHD `num_recv_frames`，預設每 5 MS/s 256 個（約 0.1 s）；0 = UHD 預設 |
| `--stats-log PATH` | 每次統計刷新附加一行 JSON，見 [2.7 節](02-gui.md#27---stats-log把統計存成檔案) |
| `--stats-log-max-mb MB` | `--stats-log` 的大小上限，預設 16 MiB，超過就輪替成 `PATH.1`；0 = 不設限 |
| `--stats-log-backups N` | 保留幾份輪替後的舊檔，預設 1 |
| `--overload-protection {off,drop,give-up,both}` | 過載保護，**預設 `off`，量測一律關閉**。`drop`：decode pool 滿時丟掉放不下的 burst，不讓 receiver 等；`give-up`：Turbo block 到第 4 次 iteration 還有超過 1/64 的 bit 在翻，就放棄。兩者各有自己的計數器，見 [7.10 節](#710-效能開關與-native-模組) |

**TX health（`tx_health`，`tx_app --stats-log` 與 `node_app` 的 `tx.health`）。** 一個平的紀錄，發送端每個會掉東西的地方各有自己的計數器
（累計值；深度是當下的值）。Transport 沒有的計數器寫 `null`，不寫 0：

| 欄位 | 意義 |
|---|---|
| `queue_datagrams` | 發送端收下、還沒完全送上空中的 datagram 數 |
| `queue_bursts` | Radio sink 裡等著送的 burst 數（UHD） |
| `ingress_dropped_queue_full` | UDP ingress 收到、但發送 queue 滿了被拒絕的 datagram（以前 `tx_app` 默默丟掉） |
| `bursts_rejected_queue_full` | Radio sink 的 burst queue 滿了而拒絕的 burst |
| `tx_underflow`、`tx_underflow_in_packet` | UHD 在 burst 之前／之中沒有 sample 可送 |
| `tx_late` | UHD 的 `tx_time_error`：burst 到 radio 時已經過了它的開始時間 |

## 7.2 設定檔

設定分兩層：[`configs/default.yaml`](../configs/default.yaml) 是基底，再疊上一個 profile
（`--overlay`，可重複，後面的蓋過前面的）。

| Profile | 內容 |
|---|---|
| [`ota_2p45ghz.yaml`](../configs/profiles/ota_2p45ghz.yaml) | **預設**。2.45 GHz、5 MS/s |
| [`usrp_ota_1p2ghz.yaml`](../configs/profiles/usrp_ota_1p2ghz.yaml) | 選用。1.2 GHz、5 MS/s。這個頻段配置給 GNSS，只在確定可以使用時才用 |
| [`b210_ota_3p8ghz.yaml`](../configs/profiles/b210_ota_3p8ghz.yaml) | 選用。3.8 GHz、5 MS/s，gain 設定很高 |
| [`b210_ota_3p8ghz_10msps.yaml`](../configs/profiles/b210_ota_3p8ghz_10msps.yaml) | 選用。同上但 10 MS/s |
| [`duplex_fdd_2420_2460.yaml`](../configs/profiles/duplex_fdd_2420_2460.yaml) | `node_app` 的預設。兩台 Pluto、5 MS/s；node 1 TX 2420／RX 2460 MHz，node 2 相反 |
| [`duplex_fdd_2405_2475.yaml`](../configs/profiles/duplex_fdd_2405_2475.yaml) | `node_app` 的備用：70 MHz 間距，2405／2475 MHz |
| [`duplex_fdd_2420_2460_15p36.yaml`](../configs/profiles/duplex_fdd_2420_2460_15p36.yaml) | `node_app`，兩台 B210、15.36 MS/s，同 2420／2460 MHz（7.4 節） |
| [`b210_2p45ghz_15p36msps.yaml`](../configs/profiles/b210_2p45ghz_15p36msps.yaml) | `tx_app`／`rx_app`，B210、15.36 MS/s、2.45 GHz（7.4 節） |
| [`legacy_rx.yaml`](../configs/profiles/legacy_rx.yaml) | **附加用**：把 `phy.rx.legacy_acquisition` 打開，也收 v1.0／`improve-rx` 的 TX 送的舊格式 burst（v2.0 起預設不收） |
| [`b210_measurement.yaml`](../configs/profiles/b210_measurement.yaml) | **附加用**，接在上面任一個 profile 後面：把 `phy.rx.legacy_acquisition` 關掉。v2.0 起預設本來就是關，它只在底下的設定把它打開時才有作用（`run_duplex.sh --transport uhd` 仍會自動加） |

這個專案實際讀取的區段：

| 區段 | 用途 |
|---|---|
| `phy` | Sample rate、center frequency、預設 MCS 與 FEC、synchronization 的門檻；`phy.v5`、`phy.rx` 見下 |
| `mac.aggregation` | v5 TX 的 aggregation 上限，見下 |
| `radio` | `adapter`（`uhd` 才能用 `--transport uhd`）、預設 gain 與 antenna、clock source |
| `acceleration` | `cpu_cores`：限制 NumPy 的 thread 數 |
| `duplex` | 只有 `node_app` 讀（選用）：`mode`、`control_mcs_index`、`nodes`，見下 |

`simulation` 區段是 PHY 測試用的 channel 模型參數。`node`、`network` 與 `mac` 的其他欄位是設定 schema
要求的，但單向 link 沒有 IP 介面、TDD 或 ARQ，所以用不到它們的值。

標「實驗」的 key 是為了 A/B 量測保留的開關，平常不要改。依據見
[`experiments/tx_v5/`](../experiments/tx_v5/README.md)。

`phy.v5`（只影響 TX）：

| Key | 預設 | 範圍 | 說明 |
|---|---|---|---|
| `guard_pre_samples` | 16 | 0–256 | v5 burst 前面的 zero samples |
| `guard_post_samples` | 64 | 0–256 | v5 burst 後面的 zero samples |
| `scrambler` | `true` | | 實驗。`false` 送 seed 0（不 scramble），payload 內容會改變 PAPR 與發射功率 |

`phy.rx`（只影響 RX）：

| Key | 預設 | 說明 |
|---|---|---|
| `legacy_acquisition` | `false`（v2.0 起） | `true`：也收 v1–v4 的舊格式 burst（v1.0 的 TX 只送舊格式）。這個 repo 的 TX 只送 v5，開著只會讓 receive owner 多花 10–16%（有 DC 時 44–46%） |
| `v5_acquisition` | `true` | 實驗。關掉 v5 acquisition |
| `v5_detection_threshold` | 0.10 | v5 的 M128 門檻（0.02–1.0），只限制工作量 |
| `v5_stf_threshold` | 0.20 | v5 STF 的 coherent 相關門檻。空中雜訊最高 0.084 |
| `v5_correlation_threshold` | 0.25 | v5 LTF 兩個 symbol 的相關門檻。空中雜訊最高 0.067 |
| `v5_balance_ratio` | 0.5 | 兩個 LTF symbol 各自的相關都要 ≥ 這個比例 × 合起來的相關；擋掉舊格式的 burst（0.05–1.0） |
| `acquisition_prefilter` | `false` | 實驗。acquisition 前先低通到資料頻帶（兩種都用） |
| `ltf_average` | `true` | 實驗。`false` 只用第一個 LTF symbol 估 channel |
| `channel_smoothing` | `true` | 實驗。`false` 用沒有平滑的 LS estimate |
| `llr_noise_source` | `max` | 實驗。`max`、`header`（SIG 量的）或 `ltf`。三者實測結果相同 |
| `max_psdu_bytes` | 16384 | SIG 可以宣告的最大 PSDU（19–65535） |
| `turbo_max_iterations` | 8 | Turbo iteration 上限（2、4、6、8） |
| `fft_backoff_samples` | 3 | 每個 v5 FFT window 提早進入 cyclic prefix 幾個 sample（0–8） |

`duplex`（只有 `node_app` 讀；`tx_app` 與 `rx_app` 忽略它）：

| Key | 預設 | 說明 |
|---|---|---|
| `mode` | 必填 | `fdd`：每個 node 有自己的 TX 頻率，兩個頻率至少相隔一個 sample rate。`lbt`（同一個頻率）只有設定 schema 與 `duplex.lbt` 的參數，`node_app` 沒有實作 listen-before-talk（M1 量到不需要） |
| `control_mcs_index` | 8 | Control-only burst 的 MCS（8–17）；`--control-mcs-index` 會蓋過它 |
| `nodes` | 必填 | 剛好兩個 `{node_id, tx_frequency, rx_frequency}`；id 不能相同，一個 node 的 `tx_frequency` 必須是另一個的 `rx_frequency`。Schema 接受 id 1–254，`node_app --node-id` 只接受 1 與 2 |

`mac.aggregation`（只影響 TX；命令列的 aggregation 選項會蓋過它）：

| Key | 預設 | 說明 |
|---|---|---|
| `enabled` | `true` | `false`：一個 burst 一個 datagram |
| `max_wait_ms` | 0.0 | 還有空間的 burst 最多等多久 |
| `min_burst_interval_ms` | 4.0 | 兩個 burst 的開始時間至少隔多久（start 到 start，0–50，0 關閉）。短 datagram 時避免每個 datagram 一個 burst：Pluto 推送一次要 0.413 ms + 0.194 µs/sample，RX 每個 burst 也有成本。`AggregationLimits` 本身的預設是 0，直接呼叫的程式碼行為不變 |
| `max_subframes` | 32（v2.0 起；之前 16） | 一個 burst 最多幾個 datagram |
| `max_psdu_bytes` | 16384（v2.0 起；之前 8192） | 一個 burst 的 PSDU 最多幾個 byte；不能超過 `phy.rx.max_psdu_bytes`（16384），否則設定檔會被拒絕。B210 的最大吞吐測試（2026-11-11）在 10 MS/s MCS 17 用 16384／32 撐住 24.0 Mb/s，8192 時是 20.7 Mb/s |
| `max_burst_airtime_ms` | 8.0 | 聚合後的 burst 最長多久。換算成 sample 不能超過 80 000（5 MS/s 是 16 ms、10 MS/s 是 8 ms、15.36 MS/s 是 5.21 ms、20 MS/s 是 4 ms），否則設定檔會被拒絕 |

為什麼是 80 000 samples：兩個 ±25 ppm 的 crystal 最多差 50 ppm，RX 的 timing 修正最多 4 個 sample，
4 ÷ 50 ppm = 80 000。

## 7.3 MCS 表

TX 送的 wire-v5 MCS：

| Index | 格式 | Spectral efficiency | PHY rate @ 5 MS/s | 996-byte frame airtime @ 5 MS/s | SNR 門檻 | TX 選單 |
|---:|---|---:|---:|---:|---:|---|
| 8 | QPSK 1/3 | 0.67 bit/symbol | 2.0 Mbit/s | 4.19 ms | 7.5 dB | 預設 |
| 9 | QPSK 1/2 | 1.00 bit/symbol | 3.0 Mbit/s | 2.85 ms | 9.0 dB | 有 |
| 10 | QPSK 2/3 | 1.33 bit/symbol | 4.0 Mbit/s | 2.16 ms | 11.0 dB | 有 |
| 11 | 16QAM 1/3 | 1.33 bit/symbol | 4.0 Mbit/s | 2.16 ms | — | 隱藏（沒有量） |
| 12 | 16QAM 1/2 | 2.00 bit/symbol | 6.0 Mbit/s | 1.49 ms | 14.0 dB | 有 |
| 13 | 16QAM 2/3 | 2.67 bit/symbol | 8.0 Mbit/s | 1.15 ms | 15.5 dB | 有 |
| 14 | 16QAM 3/4 | 3.00 bit/symbol | 9.0 Mbit/s | 1.04 ms | 17.5 dB | 有 |
| 15 | 64QAM 1/2 | 3.00 bit/symbol | 9.0 Mbit/s | 1.04 ms | 16.5 dB | 有 |
| 16 | 64QAM 2/3 | 4.00 bit/symbol | 12.0 Mbit/s | 0.82 ms | 21.0 dB | 有 |
| 17 | 64QAM 3/4 | 4.50 bit/symbol | 13.5 Mbit/s | 0.74 ms | 22.0 dB | 有 |

- 全部用同一個 LTE Turbo code，經 rate matching 得到不同的 code rate（見 7.7）。
- PHY rate = 48 data carriers × 每個 symbol 的 bit 數 × code rate ÷ 80 個 sample。Airtime 含 guard，
  是一個 burst 只帶一個 996-byte frame 時的長度；aggregation 會讓多個 frame 共用 preamble 與 SIG。
- SNR 門檻是 Pluto 對 Pluto 的實測（[8.8](08-measurements.md#88-wire-v5pluto--pluto245-ghz)）。
  MCS 15 比 MCS 14 少需要 1 dB，spectral efficiency 一樣。
- 表定義在 [`ofdm_link/phy/mcs_table.py`](../src/ofdm_link/phy/mcs_table.py)。TX 視窗依當前 sample rate
  即時計算 airtime 與 PHY rate。
- 在 TX 視窗換 MCS 不必 Stop radio，下一則開始 encode 的 message 生效。同一則 fragmented message
  固定使用同一個 MCS。RX 不需跟著操作。

舊格式（wire v1–v4）的 MCS，**只用在 RX 解 v1.0.0 或 `improve-rx` 的 TX 送的 burst**（需要 `phy.rx.legacy_acquisition: true`，v2.0 起預設關）；現在的 TX 不送：

| Index | 格式 | Spectral efficiency | 996-byte airtime @ 5 MS/s |
|---:|---|---:|---:|
| 0 | QPSK Turbo r1/3 | 0.67 bit/symbol | 4.16 ms |
| 1 | QPSK Conv r1/3 | 0.67 bit/symbol | 4.11 ms |
| 2 | QPSK Conv r1/2 | 1.00 bit/symbol | 2.77 ms |
| 3 | QPSK Uncoded r1 | 2.00 bit/symbol | 1.42 ms |
| 4 | 16QAM Turbo r1/3 | 1.33 bit/symbol | 2.13 ms |
| 5 | 16QAM Conv r1/3 | 1.33 bit/symbol | 2.10 ms |
| 6 | 16QAM Conv r1/2 | 2.00 bit/symbol | 1.42 ms |
| 7 | 16QAM Uncoded r1 | 4.00 bit/symbol | 0.75 ms |

（舊格式的 airtime 用 256 + 256 的 guard 算。各 MCS 的 OTA throughput 見
[throughput 實驗報告](../experiments/ota_udp_throughput/README.md)，那份是舊格式的數據。）

### 為什麼 RX 不必知道 MCS、但 sample rate 必須一致

每個 burst 都自己說明格式。v5 的 SIG 帶 MCS index、PSDU 長度與 scrambler seed（見 7.7）；舊格式的
header 帶 magic、wire version、payload modulation 與 length，經 3 次 bit repetition 與 CRC 保護。
RX 先解 header，再選 payload 的 demapper 與 decoder。

Sample rate 不在 header 裡。兩端不同，等於對「一個 sample 代表多少時間」有不同解讀，連 preamble
都對不上。

舊格式的 wire version 是 FEC 種類的編號：v1 = convolutional r1/2、v2 = Turbo r1/3、v3 = convolutional r1/3、
v4 = uncoded。它和 modulation 一起決定 MCS index。v5 的 SIG 直接帶 MCS index。

## 7.4 Analog bandwidth 與 OFDM signal 寬度

Radio 的 analog filter bandwidth 設成等於 sample rate。這和 OFDM signal 實際佔用的寬度不一樣。

這條規則適用 `tx_app` 的 TX 與 `rx_app` 的 RX（`options.radio_settings`），不能調：overlay 的 `radio.bandwidth` 對它們
**沒有作用**，B210 讀回的值仍等於 sample rate。這樣切換 sample rate 時 filter 一定跟著變。`node_app` 的 TX 因為 LO 偏離載波，
預設較寬，可以用 `--tx-analog-bw` 調（見 7.9 節）。2026-11-12 評估過讓 `tx_app` 也可調：沒有量測顯示需要（`node_app` 用
5 MHz 的 TX filter 也正常），設太寬只會增加帶外發射，所以不做。

這個 PHY 使用 64-point FFT，佔用的 subcarrier 是 −26…−1 與 +1…+26（48 data + 4 pilots，DC 為空），
所以 occupied bandwidth 是 `52/64 × Fs = 0.8125 Fs`：

| Sample rate | OFDM occupied bandwidth | Analog bandwidth |
|---:|---:|---:|
| 5 MS/s | 4.06 MHz | 5 MHz |
| 10 MS/s | 8.13 MHz | 10 MHz |
| 15.36 MS/s（B210 限定） | 12.48 MHz | 15.36 MHz |
| 20 MS/s | 16.25 MHz | 20 MHz |

**15.36 MS/s（只有 B210，`--transport uhd`）。** LTE 的 sample rate：B210 的 master clock 固定設成 30.72 MHz，FPGA 剛好 2 倍
decimation（`ofdm_link.radio.uhd.MASTER_CLOCK_RATES`；30.72 MHz 也是 B210 兩個 channel 同時用時允許的最高值，`node_app` 一台
裝置 TX 與 RX 各用一個前端）。其他速率仍由 UHD 自己選 master clock。用 profile
[`duplex_fdd_2420_2460_15p36.yaml`](../configs/profiles/duplex_fdd_2420_2460_15p36.yaml)（`node_app`）或
[`b210_2p45ghz_15p36msps.yaml`](../configs/profiles/b210_2p45ghz_15p36msps.yaml)（`tx_app`／`rx_app`）。這個速率下：
aggregate 的 airtime 上限要 ≤ 5.21 ms（80 000 samples），profile 設 5 ms，沿用 8 ms 預設值的設定檔會被拒絕；RX 的 UHD
receive frames 與 chunk 依 sample rate 放大到 1024 個 frame 與 4 × 65536 samples；`node_app` 的 TX analog filter 預設是
35 MHz（見 7.9 節的 `--tx-analog-bw`）。Pluto 不支援。2026-11-11 在兩台 B210 上量過單向 MCS 17：約 35.5 Mb/s，上限是 TX airtime。12.5 MS/s 沒有開放。

把 analog bandwidth 設成 Fs 會在 signal 兩側留下合理的 transition margin。設得太窄會先衰減外緣的
subcarrier、提高 EVM；設得太寬則會收進更多帶外 noise 與鄰近 channel 的干擾。

以上是從 subcarrier 配置與 filter 的用途推導的，**沒有用硬體量測過 EVM 隨 bandwidth 的變化**。
這是一個可以做的實驗題目。

## 7.5 為什麼用自己的 PHY，不用 gr-digital 的 `ofdm_tx` / `ofdm_rx`

GNU Radio 內建一組 OFDM blocks。這個專案沒有用它們，而是用 `ofdm_link.phy` 裡自己寫的 PHY，原因：

- **看得到每一步。** `encode_burst` 與 `decode_burst` 是一般的 Python 函式，輸入輸出都是 NumPy
  array，可以單獨呼叫、單獨測試、在任何一步把中間結果印出來或畫出來。Flowgraph 裡的 block
  很難這樣拆開來看。
- **FEC 與 header 是一起設計的。** Turbo decoder（C++）、pilot-based channel estimation、burst
  acquisition、CFO／sample clock offset 補償，以及受保護的 header 都在同一套 wire format 裡。
- **PHY 只處理 bytes。** 上層不需要知道 GNU Radio 的 tag 或 PDU。

GNU Radio 在這裡的角色是 **radio 的介面**（UHD source／sink、timed burst）以及舊格式 convolutional MCS
（1、2、5、6，只有 RX）的 Viterbi decoder。v5 的 SIG 用專案自己的 C++ Viterbi。

同理，application 與 PHY 之間用一般的 UDP socket，而不是 gr-network 的 block：任何語言寫的程式
都能接上。

## 7.6 測試涵蓋什麼

```bash
python -m pytest -q                 # 全部
python -m pytest tests/phy -q       # 只測 PHY
python -m pytest tests/app -q       # 只測應用程式
```

全部 headless、無硬體、無 display、無 root。

`tests/phy/`：FEC 的 encode／decode、Turbo code、rate matching、symbol mapping 與 soft demapping（含 64QAM）、
OFDM 調變、兩種 preamble 的偵測、channel estimation、burst header 與 SIG、scrambler、A-MPDU，以及完整
burst 在有 noise、multipath、CFO、DC、sample clock offset 的模擬 channel 下的 round-trip。舊格式與 v5
混在同一個 stream 裡時，兩種都要解出來；舊格式的結果由一個 golden test 固定住。

`tests/app/`：

- Datagram framing、fragmentation 與 reassembly，以及不完整 message 的丟棄。
- Sequence gap、16-bit wraparound、out-of-order 的判定。
- 完整 waveform 往返，以及**連續 noise stream 中的 burst 偵測**（刻意用非對齊的 chunk 餵入）。
- Peak normalization 的不 clipping 保證。
- RF 授權閘門：缺 `--enable-rf` 或 acknowledgement 不符時必須拒絕。
- Pluto 與 USRP 的列舉、probe 與設定轉換（用假的裝置物件驅動，不需要硬體）。
- Link light 的每一條規則與邊界值。
- 多 worker 的 payload decode 與單 worker 的結果逐 byte 相同。
- 兩個視窗的建構，以及**用 README 的指令實際啟動 app 的 subprocess 測試**。

修改 PHY 之後，除了跑測試，也請重新產生互動導覽的資料：`python -m guide.build_data`。

## 7.7 Wire format v5

### Burst

Offset 從 STF 的第一個 sample 算起（5 MS/s 時 1 sample = 0.2 µs）：

| 部分 | Offset | Samples | 內容 |
|---|---:|---:|---|
| Guard（前） | −16 | 16 | Zeros（`phy.v5.guard_pre_samples`） |
| STF | 0 | 160 | 802.11a L-STF：carrier ±4、±8 … ±24，16-sample 的週期重複 10 次 |
| LTF | 160 | 160 | 802.11a L-LTF：32-sample guard + 兩個相同的 64-sample symbol（L1 在 192，L2 在 256） |
| SIG | 320 | 240 | 3 個 OFDM symbol，BPSK，pilot 的 symbol index 0–2 |
| Payload | 560 | 80 × N | N 個 OFDM symbol，pilot 的 symbol index 從 3 開始 |
| Guard（後） | 560 + 80N | 64 | Zeros（`phy.v5.guard_post_samples`） |

- 每個 OFDM symbol 80 個 sample（16 CP + 64）；48 個 data carrier、4 個 pilot（±7、±21）。
- STF、LTF 與資料的平均功率相同（52/64）。
- 固定部分（STF + LTF + SIG）是 560 個 sample；舊格式是 400。
- 整個 burst 一次推給 radio，發射前把 peak 縮放到 `--peak-amplitude`。

### SIG

48-bit body，MSB 先送：

| Bits | 欄位 | 寬度 | 值 |
|---|---|---:|---|
| 47:44 | `format_version` | 4 | 0；其他值 RX 拒絕 |
| 43:38 | `mcs_index` | 6 | v5 的 MCS index（8–17）；舊格式的 0–7 與未知的值 RX 拒絕 |
| 37:22 | `psdu_length` | 16 | PSDU 的 byte 數，含 4-byte FCS；RX 接受 19 到 `phy.rx.max_psdu_bytes` |
| 21:15 | `scrambler_seed` | 7 | 1–127；0 = 沒有 scramble（實驗） |
| 14:13 | `payload_format` | 2 | 0 = A-MPDU；1–3 保留，RX 拒絕 |
| 12:0 | 保留 | 13 | 0；其他值 RX 拒絕 |

- 72-bit info word = body（48）+ CRC-16/CCITT-FALSE（16，poly 0x1021、init 0xFFFF，算 6 個 body byte）
  + `00`（2）+ tail `000000`（6）。
- K=7、generator 133/171（octal）的 rate-1/2 convolutional code → 144 bit。
- Block interleaver：coded bit `k` 放到第 `9 × (k mod 16) + ⌊k / 16⌋` 格；第 `i` 格是 SIG symbol `⌊i / 48⌋`
  的第 `i mod 48` 個 data carrier。
- BPSK：bit 0 → +1。SIG 不 scramble。
- 任何一項檢查失敗就是 header 失敗，**沒有 ML list 的備援**（舊格式有）。

### PSDU 與 A-MPDU

```
PSDU     = SF_1 │ pad_1 │ SF_2 │ pad_2 │ … │ SF_n │ FCS          （SF_n 後面沒有 pad）
SF_i     = delimiter（4 B）│ MPDU_i
MPDU_i   = 一個 serialized frame：7-byte header（含 16-bit sequence number）│ datagram │ CRC-32
pad_i    = 0–3 個 zero byte，讓每個 delimiter 從 4 的倍數開始
FCS      = CRC-32（zlib），算前面全部的 PSDU byte，big-endian
```

Delimiter：

| Byte | 內容 |
|---|---|
| 0–1 | bit 15:14 保留（0）、bit 13:0 MPDU 長度（11–16383），big-endian |
| 2 | CRC-8，算 byte 0–1（poly 0x07、init 0xFF） |
| 3 | Signature 0x4E |

最小的 PSDU 是 19 byte（delimiter 4 + 最短的 frame 11 + FCS 4）。

### Payload 的 coding

1. PSDU（含 FCS）共 `B = 8L` bit，照 TS 36.212 §5.1.2 分成 code block（多於一個 block 時每個加 CRC-24B）。
2. 原本的 native Turbo encoder（rate 1/3）。
3. Rate matching（TS 36.212 §5.1.4.1，RV0）：sub-block interleaver、bit collection、circular buffer，
   讀出 `G = 48 × Qm × N` 個 bit。`N = ⌈B × R 的分母 ÷ (R 的分子 × 48 × Qm)⌉`，Qm 是每個 symbol 的 bit 數。
4. Scrambler：802.11 的 x⁷+x⁴+1，初始狀態 = SIG 的 seed，作用在全部 G 個 bit 上。
5. Gray mapping 到 QPSK、16QAM 或 64QAM，依 carrier 由小到大、symbol 一個接一個填。

## 7.8 雙向 link 的 control 與 feedback wire format

只有雙向的 `node_app` 會送 control frame。`tx_app` 與 `rx_app` 沒有設定 hook 時，送出與接收的內容
都和以前一樣（`tests/app/test_tx_policy.py` 用固定 seed 比對 burst 的 hash）。

**位置。** Node 的每個 v5 burst 的第一個 MPDU 是一個 `FrameKind.CONTROL` frame：`mcs` = 該 burst
的 modulation，`sequence` 來自一個**獨立的** 16-bit control counter，payload 是一個 Link Control PDU
（LCP）。Reporter 有 report 到期時 LCP 是 FEEDBACK，否則是只有 header 的 LINK_HEADER。DATA frame 的
sequence 因此保持連續，舊版 `rx_app` 不會看到假的 gap，只把 CONTROL frame 記在 `control_frames`。
沒有 data 而 report 到期時，node 送一個只含 CONTROL frame 的 control-only burst（`--control-mcs-index`，
預設 MCS 8）；沒有 data 也沒有 report 時什麼都不送。兩個 node 用不同的 scrambler seed 範圍：node 1 用
1–63，node 2 用 64–127（`tx_app` 仍用 1–127）；RX 由 SIG 的 seed 在 payload decode 之前丟掉自己 TX 的洩漏。

**LCP header（8 bytes，big-endian）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | magic | u16 | `0x4C43`（"LC"） |
| 2 | version | u8 | 1；其他版本整個 LCP 被忽略 |
| 3 | pdu_type | u8 | 1 LINK_HEADER、2 FEEDBACK、3 STATUS（保留給 ARQ）；不認得的 type 被忽略 |
| 4 | src_node | u8 | 1–254；等於自己的 id 時，整個 PSDU 是自己 TX 的洩漏，丟棄（seed 範圍之外的第二道檢查） |
| 5 | dst_node | u8 | 0 = 任何 node；node 送 peer 的 id。不是 0 也不是自己時，內容被忽略（`control_other_dst`） |
| 6 | flags | u8 | b0 CONTROL_ONLY（control-only burst）、b1 LBT_MODE（保留，沒有使用）、b2 TX_QUEUE_NONEMPTY（有 data 的 burst） |
| 7 | body_length | u8 | LINK_HEADER 為 0 |

**FEEDBACK body v1（36 bytes 固定部分）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | report_seq | u16 | 每份 report +1（mod 2^16）；TX 的 `FeedbackTracker` 忽略晚到或重複的 report，peer 重新啟動時重設基準 |
| 2 | window_ms | u16 | SNR 統計的時間窗 |
| 4 | age_ms | u16 | 時間窗結束到 encode 的時間（飽和） |
| 6 | preambles | u16 | 時間窗內 peer 的所有 preamble |
| 8 / 10 / 12 | snr_mean / p10 / last | i16 | 0.01 dB；`0x8000` = 無 |
| 14 | data_ok_total | u32 | 累計 CRC 正確的 DATA subframe |
| 18 | data_lost_total | u32 | 累計 DATA sequence gap |
| 22 | last_data_seq | u16 | fb_flags b0 = 1 時才有效 |
| 24 / 26 / 28 | psdu_fcs_fail / header_fail / rx_overflow total | u16 | 累計，mod 2^16 |
| 30 | last_mcs_index | u8 | `0xFF` = 無 |
| 31 | fb_flags | u8 | b0 last_seq_valid、b1 clipping_seen、b2 decode_backlog |
| 32 | reporter_id | u8 | 0 = 內建 reporter |
| 33 | n_entries | u8 | ≤ 16 |
| 34 | tlv_length | u16 | entries 後面 TLV 的 bytes |
| 36 | entries | 6 B 各一 | seq_first u16、mcs u8（`0xFF` = 無）、snr u8（0.25 dB 一階，0–63.5 dB 飽和；`0xFF` = 無）、ok u8、lost u8（飽和在 255）；只留最近的 16 個 |
| … | TLVs | | type u8、len u8、value。0x10 SESSION_ID：len 4、u32，reporter 啟動時隨機抽，encode 時永遠放第一個；peer 的 session id 改變時 TX 立即重設基準。0x80 以下保留給這個 codec，0x80–0xFF 給自訂 reporter；不認得的 TLV 放進 `extras` |

- 計數器是累計值，所以掉了幾份 feedback 也不會遺失資訊：TX 把收到的兩份相減。
- 解碼端遇到任何格式錯誤都回傳 `None`，不會丟例外；body 比已知的長時，多出的部分被忽略。
- 成本（在 burst 裡，含 frame header、CRC、delimiter 與 padding）：LINK_HEADER 每個 burst 24 bytes；
  內建 reporter 的 FEEDBACK（含 6-byte 的 session id TLV）沒有 entry 時 68 bytes、4 個 entries 92 bytes、
  8 個（內建 reporter 的上限）116 bytes。4 個 entries 的 control-only burst 在 MCS 8 是 2 560 samples（0.51 ms）。
- Data 的 MCS 高於 9 時，每 100 ms 至少有一份 feedback 走 MCS ≤ 9 的 burst（在 data burst 之前送一個
  control-only burst），data 的 MCS 太高時 feedback 仍然到得了。
- `pdu_type 3 STATUS` 保留給 ARQ，見[提案](../experiments/duplex_fdd/README.md#5-arq-提案沒有實作)。
- Codec 在 [`ofdm_message_link/control.py`](../src/ofdm_message_link/control.py)，report 的資料結構在
  [`ofdm_message_link/la/api.py`](../src/ofdm_message_link/la/api.py)。

## 7.9 `node_app`：雙向 node 的命令列

架構圖見 [1.7 節](01-architecture.md#17-雙向-nodenode_app)。一個 Pluto（或一台 USRP B210）一個 process。Node 同時發送與接收（FDD）：node 1 在 2420 MHz 發、2460 MHz 收，node 2
相反（[`configs/profiles/duplex_fdd_2420_2460.yaml`](../configs/profiles/duplex_fdd_2420_2460.yaml)，
備用 `duplex_fdd_2405_2475.yaml`）。一個 Pluto 只能開一個 libiio context（第二個回 EBUSY），所以 TX 與
RX 共用一個 `pluto.SharedPlutoContext`，在同一個 engine 子 process 裡。B210（`--transport uhd`）用同一台
裝置的兩個前端：TX 在一個（預設 RF A，TX/RX port），RX 在另一個（預設 RF B），各是一個 UHD block，
以 `serial=` 選裝置（`node_engine.UhdDuplexFactory`）。

```bash
# 視窗模式
python -m ofdm_message_link.node_app --node-id 1 --transport pluto \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
# Headless，給腳本量測用
python -m ofdm_message_link.node_app --node-id 1 --transport pluto --headless \
    --serial 104473541196000419000c00f2ff1e6e77 --tx-gain -5 --duration 90 \
    --gain-profile 0:-5,30:-20,60:-5 --link-adaptation olla \
    --stats-log node1_stats.jsonl --la-log la_1.jsonl --ready-file node1.ready \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
# USRP B210（gain 是 B210 的放大器 gain，不是 Pluto 的衰減）
UHD_IMAGES_DIR=/usr/share/uhd/images python -m ofdm_message_link.node_app --node-id 1 \
    --transport uhd --headless --serial 30FE782 --tx-gain 65 --rx-gain 30 --duration 120 \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
# 不用 radio：兩個 node 在同一台 PC 上經 UDP 交換 samples
python -m ofdm_message_link.node_app --node-id 1 --headless &
python -m ofdm_message_link.node_app --node-id 2 --headless &
```

**Ports**（`base = 52200 + 10·node_id`，不與 52001／52002／52012／52022／52101 衝突）：

| Node | ingress | egress | control | UDP sample |
|---:|---:|---:|---:|---:|
| 1 | 52211 | 52212 | 52213 | 52214 |
| 2 | 52221 | 52222 | 52223 | 52224 |

Ingress 與 control socket 都不設 `SO_REUSEADDR`：port 已被佔用時直接 EADDRINUSE 失敗，不會兩個
process 默默分流。

| 選項 | 說明 |
|---|---|
| `--node-id {1,2}` | 必填；決定 profile 中的頻率、ports 與 scrambler seed 範圍（1–63／64–127） |
| `--transport {udp,pluto,uhd}` | 預設 `udp`（不用 radio）；`pluto` 一台 Pluto；`uhd` 一台 USRP B210（TX、RX 各用一個前端）。`udp` 用 `--udp-host`（預設 127.0.0.1）與下面的 sample ports，`--udp-port` 對 `node_app` 沒有作用 |
| `--overlay` | 預設 `configs/profiles/duplex_fdd_2420_2460.yaml`；必須有 `duplex` 段 |
| `--serial` / `--auto-start` | `pluto`／`uhd`：選哪一台 Pluto 或 USRP；`--headless` 必須給 `--serial`，而且隱含 `--auto-start` |
| `--channel` / `--antenna` | `uhd`：RX 的前端（0 = RF A，1 = RF B，預設 1；TX 用另一個）與 RX port（`TX/RX` 預設，或 `RX2`）；TX 一律 `TX/RX` |
| `--tx-lo-offset HZ` / `--tx-analog-bw HZ` | `uhd`：TX LO 離載波的距離（預設 8e6，`tune_request`；0 = 不偏移）與 TX analog filter。沒有偏移時 TX 的 LO 洩漏落在對方 RX 中心約 2.4 kHz，淹沒 acquisition。其他 transport 給這兩個會被拒絕。TX filter 的預設值依 sample rate 與 LO offset 計算（見下表）；給 `--tx-analog-bw 25e6` 就是 v2.0 以前的設定 |
| `--tx-freq` / `--rx-freq` | 覆寫 profile 的頻率；信號邊緣離開 2400–2483.5 MHz 時警告 |
| `--tx-gain` / `--rx-gain` | Pluto／udp：TX −89.75–0 dB（衰減），預設 −89.75 / 15 dB。B210（`uhd`）：TX 0–89.75 dB、RX 0–76 dB，預設 0 / 30 dB；兩種尺度不能互用。`--gain` 會被拒絕（node 有兩個 gain） |
| `--rx-analog-bw HZ` | AD9361 RX analog filter，預設 = sample rate |
| `--rx-kernel-buffers N` | libiio RX kernel buffers，預設 4 |
| `--tx-duty-cap D` | `pluto`：TX duty 不超過 D（0.05–1），預設 0.7（USB 2.0 同時帶 5 MS/s RX 時的上限，見 M0）；1 = 不限。`udp` 沒有 duty cap |
| `--peak-amplitude` | 每個 burst 縮放到這個 peak，預設 0.7 |
| `--snr-db` | 只有 `udp`：在 RX 加 AWGN |
| `--headless` | 不開視窗，隱含 `--auto-start`；`pluto` 與 `uhd` 需要 `--serial` |
| `--duration S` | Ready 後 S 秒停止（headless） |
| `--gain-profile "T:DB,..."` | 依 ready 後的秒數排程 TX gain，例如 `0:-5,30:-20,60:-5`；gain 依 `--transport` 的 TX 範圍檢查 |
| `--control-port P` | localhost UDP JSON control socket，預設依 node；0 = 關閉 |
| `--link-adaptation SPEC` | `olla`（預設，兩個 loop，從 `--mcs-index` 開始）、`fixed`（固定在 `--mcs-index`）、`arf`、`pkg.mod:Class`、`file.py:Class` |
| `--link-adaptation-arg K=V` | LA class 的參數，可重複 |
| `--feedback-reporter SPEC` / `--feedback-reporter-arg K=V` | RX 端量測與回報模組，預設 `default` |
| `--mcs-index N` | 起始的 MCS（`fixed` 時是固定的 MCS），預設 8 |
| `--control-mcs-index N` | Control-only burst 的 MCS，預設 profile 的值（8） |
| `--decode-workers N` | 1–64，預設 3（兩個 node 在同一台 PC 時的 CPU 預算） |
| `--stats-log PATH` | 每 0.5 s 一行 JSON：`tx`、`rx`、`la`、`feedback`、`cpu`、`device`；control socket 的 `mark` 與 gain profile 的每一步也寫入。`tx.health` 是 TX health（見 7.1 節的 `tx_health`）。`wall_time` 是 Unix 時間（秒），`monotonic_s` 是 monotonic 時間。`tx.goodput_bps`、`tx.airtime_frac` 是 2 秒視窗，`tx.goodput_avg_bps`、`tx.airtime_avg_frac` 是第一行以來的平均。`tx.bandwidth_hz`、`rx.bandwidth_hz` 是 UHD 讀回的類比濾波器頻寬（沒有讀回值時是 `null`）。`--stats-log-max-mb`／`--stats-log-backups` 與 2.7 節相同 |
| `--la-log PATH` | 每個 LA decision 與 feedback report 一行 JSON |
| `--ready-file PATH` | 兩個方向都在跑時建立，停止時刪除 |
| `--ingress-port` / `--egress-host` / `--egress-port` | 覆寫依 node 的預設 |
| `--udp-listen-port` / `--udp-peer-port` | `--transport udp` 的 sample ports，預設自己與 peer 的 base+4 |
| `--enable-rf` / `--acknowledgement` | `--transport pluto` 必要，和 `tx_app` 相同 |
| Aggregation | `--aggregation`、`--aggregation-max-subframes`、`--aggregation-max-wait-ms`、`--min-burst-interval-ms`、`--max-burst-airtime-ms`，與 `tx_app` 相同（7.1 節） |
| 其他共用的選項 | `--config`、`--mcs`、`--frame-payload-bytes`、`--ui-fps`、`--log-lines`、`--geometry`、`--channel`、`--antenna`，與 7.1 節相同 |
| 效能開關 | `--cpu-affinity`、`--realtime[=PRIO]`、`--force`、`--native-backend`、`--overload-protection`，與 7.1 節相同（7.10 節）；全部預設關閉 |

**TX analog filter 的預設值（`node_engine.default_uhd_tx_bandwidth`）。** LO 偏離載波 `lo` 時，取樣的頻帶離 LO 最遠是
`|lo| + fs/2`，濾波器的半寬要蓋住它：頻寬 ≥ `2|lo| + fs`，再乘 1.1 的餘量（AD9361 濾波器的 roll-off），進位到整數 MHz，
最小 25 MHz、最大 56 MHz。`tx_app` 不偏移 LO，頻寬等於 sample rate，沒有這個問題。

| Sample rate | 信號離 LO 最遠（lo = 8 MHz） | 舊預設（v2.0 以前） | 新預設 |
|---:|---:|---:|---:|
| 5 MS/s | 10.5 MHz | 25 MHz | 25 MHz（不變） |
| 10 MS/s | 13 MHz | 25 MHz（半寬 12.5 MHz，切到邊緣） | 29 MHz |
| 15.36 MS/s | 15.68 MHz | —（之前不支援） | 35 MHz |
| 20 MS/s | 18 MHz | 25 MHz（切到邊緣） | 40 MHz |

10 MS/s 的結果因此可能與 2026-11-11 的 baseline 不同；硬體驗收會在最好的 10 MS/s 點比較 `--tx-analog-bw 25e6`（舊）與預設（新）。

**Control socket**：每個 datagram 一個 JSON request，回一個 JSON answer。

| Request | 動作 |
|---|---|
| `{"cmd":"set_gain","dir":"tx","db":-20}` | 立即改 gain，回 `{"ok":true,"readback":-20.0}` |
| `{"cmd":"set_mcs","mcs":12}` | 固定 MCS；`"mcs":null` 交回 LA |
| `{"cmd":"set_la","mode":"auto"}` | `auto` 或 `fixed`（凍結目前的 MCS） |
| `{"cmd":"mark","text":"phase 2"}` | 同時寫入 stats log 與 LA log |
| `{"cmd":"stats"}` / `{"cmd":"stop"}` | 最近一行 stats／停止 |

Headless 收到 SIGTERM／SIGINT、`stop` 或 `--duration` 到時：先停 TX（attenuator 回到最小輸出），再停 RX，
刪除 ready file，exit 0；radio 啟動失敗或 control port 已被佔用時 exit 2，engine 子 process 中途結束時 exit 3。

LA 演算法（預設演算法 `olla` 的參數、ladder）與怎麼寫自己的模組見
[3.4 節](03-your-own-app.md#34-寫你自己的-link-adaptation-模組)；設計、自干擾與實機量測見
[`experiments/duplex_fdd/`](../experiments/duplex_fdd/README.md) 與 [8.9 節](08-measurements.md#89-雙向-fdd兩個-node_apppluto--pluto24202460-mhz)。

**USB 限制（M0，`experiments/duplex_fdd/results/usb/`）**：Pluto 同時以 5 MS/s 接收時，TX duty 到
0.7 都沒有 overflow；要求 0.8 以上時 TX 只到 0.78，其中一個 Pluto 每分鐘 overflow 約 180–220 次。
8 個 kernel buffers 沒有改善。

## 7.10 效能開關與 native 模組

**Native 模組。** `pip install -e .`（或 `python setup.py build_ext --inplace`）編譯四個 C++17 extension，
每個只負責一件事，Python 函式的介面都沒變：

| 模組 | 取代的 Python 實作 | 用在 |
|---|---|---|
| `_acq_native` | `sync.acquire_all` 底下兩種 preamble 的偵測：lag-32 相關、DC／tone 的 steady 修正、legacy coherent、v5 M128 gate、NMS、STF coherent、LTF 精細時序與 CFO | Receive owner |
| `_header_native` | v5 header probe：LTF pair 量測、channel smoothing、SIG 等化，交出 SIG LLR（`channel.py` 沒改，workers 照舊） | Receive owner |
| `_turbo_native` | Turbo decoder：新的 float32 kernel（`decode`）；原本的 double kernel 保留為 `decode_reference` | Decode workers |
| `_tx_native` | `rate_match`、`map_symbols`、`modulate_ofdm` | 發送端（仍在 engine 裡） |

全部計算時都會放開 GIL。編譯只用 `-O3 -std=c++17 -ffp-contract=off`，沒有 `-march` 或任何 ISA 旗標，同一份原始碼在
aarch64（Jetson）也能編。

**Backend 開關。** 每一塊都可以選 native 或 Python 參考實作，用環境變數（engine 與 decode workers 會繼承）：

| 變數 | 值 |
|---|---|
| `OFDM_LINK_NATIVE` | `auto`（預設）、`python`、`native`，套用到全部；`python` 會蓋過下面每一個 |
| `OFDM_LINK_NATIVE_ACQ`、`_HEADER`、`_TURBO`、`_TX` | 只設一塊 |

`auto`：模組 import 失敗就退回 Python，警告一次。`native`：import 失敗就報錯。Turbo 的 `python` 是原本的 double kernel
（沒有 Python 版的 max-log decoder）。`--native-backend` 會在任何子 process 啟動前設好 `OFDM_LINK_NATIVE`。等價性由
`tests/phy/test_acq_native.py`、`test_header_native.py`、`test_turbo_fast.py`、`test_tx_native.py` 與
`tests/app/test_native_end_to_end.py` 檢查（容差：起點相同、|ΔCFO| ≤ 1e-6、confidence 相對誤差 ≤ 1e-5、header 欄位相同、
Turbo BLER 差 ≤ 0.1 dB 且 iteration ±1、固定 seed 的端到端 delivered 相同）。

**接收端的其他改變（不需要開關）。** `ReceiveWorker` 會把 queue 裡已經在等的短 chunk 併成至少 8192 samples 再 feed，
最多為此等 2 ms；`measure_quality` 改成每軸各自找最近點，16QAM soft demap 改成每軸分開算，輸出與原本相同。

**CPU 放置與排程（全部預設關閉）。**

- `--cpu-affinity CPUS` 在 `main` 一開始就套用，engine、forkserver、decode workers 都繼承。不寫死任何核心編號。
- `--realtime[=PRIO]`（`ofdm_link/runtime/realtime.py`）：engine process 的每個 thread 改成 SCHED_RR，加上
  SCHED_RESET_ON_FORK，所以 decode workers 與之後產生的 process 都是一般排程；radio attach 之後新開的 thread 會補上。
  優先權限 1–49，低於 kernel 的 IRQ thread（50）。不會關閉 RT throttling（`sched_rt_runtime_us`）。可用 CPU（套用
  `--cpu-affinity` 之後）少於 4 個時拒絕，除非 `--force`；非 Linux 或沒有權限（`ulimit -r`）時不做事，只記一行 log。
  離線測試（2 個 E-core）中整棵 process tree 用 `chrt -f 50` 反而遺失變多，其他程式最多卡 0.95 s，所以只開到這裡。

**過載保護 `--overload-protection`（預設 `off`）。** 只有在 CPU 不夠、寧可讓遺失平緩增加也不要整個崩潰時才用：

| 模式 | 做什麼 | 計數器 |
|---|---|---|
| `drop` | Decode pool 滿時，丟掉放不下的 segment，receive owner 不等 | `decode_pool.dropped_full` |
| `give-up` | Turbo block 到第 4 次 iteration 時，hard decision 還有超過 1/64 在變，就停止（reason `abandoned`） | decoder 的 `payload_abandoned` |
| `both` | 兩者都開 | 兩者 |

被丟掉或放棄的 burst **不算**進 `payload_decode_attempts`、`payload_decode_failure`、`crc_failures`、`psdu_fcs_failures`，
所以遺失與 BLER 的定義在保護關閉時不變。**量測（包含 JMS1 的 SNR／EVM 與遺失量測）一律關閉**；打開時程式會印一行提醒。

回到 [README](../README.md)。

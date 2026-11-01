# 7. 參考資料

## 7.1 命令列參數

`python -m ofdm_message_link.tx_app --help` 與 `rx_app --help` 會列出完整說明。

### 兩支程式共用

| 選項 | 說明 |
|---|---|
| `--config` / `--overlay` | 設定檔，預設 `configs/default.yaml` + `configs/profiles/ota_2p45ghz.yaml`；`--overlay` 可重複 |
| `--transport {udp,uhd,pluto}` | 預設 `udp`（模擬）；`uhd` 是 USRP，`pluto` 是 ADALM-Pluto |
| `--mcs {qpsk,qam16}` | TX 的簡寫：`qpsk` = MCS 8（QPSK 1/3）、`qam16` = MCS 12（16QAM 1/2）；同時給 `--mcs-index` 時後者優先。RX 僅作 display hint，不參與 decode |
| `--mcs-index N` | TX 啟動時的 wire-v5 MCS，8–17，**預設 8**（給 `--mcs qam16` 時是 12）。11 是選單隱藏的實驗用 MCS。舊格式的 0–7 TX 會拒絕；RX 仍然解舊版 TX 送的 0–7 burst。RX 接受 0–17，僅作 display hint |
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

### 只有發送端

| 選項 | 說明 |
|---|---|
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

## 7.2 設定檔

設定分兩層：[`configs/default.yaml`](../configs/default.yaml) 是基底，再疊上一個 profile
（`--overlay`，可重複，後面的蓋過前面的）。

| Profile | 內容 |
|---|---|
| [`ota_2p45ghz.yaml`](../configs/profiles/ota_2p45ghz.yaml) | **預設**。2.45 GHz、5 MS/s |
| [`usrp_ota_1p2ghz.yaml`](../configs/profiles/usrp_ota_1p2ghz.yaml) | 選用。1.2 GHz、5 MS/s。這個頻段配置給 GNSS，只在確定可以使用時才用 |
| [`b210_ota_3p8ghz.yaml`](../configs/profiles/b210_ota_3p8ghz.yaml) | 選用。3.8 GHz、5 MS/s，gain 設定很高 |
| [`b210_ota_3p8ghz_10msps.yaml`](../configs/profiles/b210_ota_3p8ghz_10msps.yaml) | 選用。同上但 10 MS/s |

這個專案實際讀取的區段：

| 區段 | 用途 |
|---|---|
| `phy` | Sample rate、center frequency、預設 MCS 與 FEC、synchronization 的門檻；`phy.v5`、`phy.rx` 見下 |
| `mac.aggregation` | v5 TX 的 aggregation 上限，見下 |
| `radio` | `adapter`（`uhd` 才能用 `--transport uhd`）、預設 gain 與 antenna、clock source |
| `acceleration` | `cpu_cores`：限制 NumPy 的 thread 數 |

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
| `legacy_acquisition`、`v5_acquisition` | `true` | 實驗。關掉其中一種 acquisition |
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

`mac.aggregation`（只影響 TX；命令列的 aggregation 選項會蓋過它）：

| Key | 預設 | 說明 |
|---|---|---|
| `enabled` | `true` | `false`：一個 burst 一個 datagram |
| `max_wait_ms` | 0.0 | 還有空間的 burst 最多等多久 |
| `min_burst_interval_ms` | 4.0 | 兩個 burst 的開始時間至少隔多久（start 到 start，0–50，0 關閉）。短 datagram 時避免每個 datagram 一個 burst：Pluto 推送一次要 0.413 ms + 0.194 µs/sample，RX 每個 burst 也有成本。`AggregationLimits` 本身的預設是 0，直接呼叫的程式碼行為不變 |
| `max_subframes` | 16 | 一個 burst 最多幾個 datagram |
| `max_psdu_bytes` | 8192 | 一個 burst 的 PSDU 最多幾個 byte；不能超過 `phy.rx.max_psdu_bytes`，否則設定檔會被拒絕 |
| `max_burst_airtime_ms` | 8.0 | 聚合後的 burst 最長多久。換算成 sample 不能超過 80 000（5 MS/s 是 16 ms、10 MS/s 是 8 ms、20 MS/s 是 4 ms），否則設定檔會被拒絕 |

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

舊格式（wire v1–v4）的 MCS，**只用在 RX 解 v1.0.0 或 `improve-rx` 的 TX 送的 burst**；現在的 TX 不送：

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

這個 PHY 使用 64-point FFT，佔用的 subcarrier 是 −26…−1 與 +1…+26（48 data + 4 pilots，DC 為空），
所以 occupied bandwidth 是 `52/64 × Fs = 0.8125 Fs`：

| Sample rate | OFDM occupied bandwidth | Analog bandwidth |
|---:|---:|---:|
| 5 MS/s | 4.06 MHz | 5 MHz |
| 10 MS/s | 8.13 MHz | 10 MHz |
| 20 MS/s | 16.25 MHz | 20 MHz |

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


回到 [README](../README.md)。

# 7. 參考資料

## 7.1 命令列參數

`python -m ofdm_message_link.tx_app --help` 與 `rx_app --help` 會列出完整說明。

### 兩支程式共用

| 選項 | 說明 |
|---|---|
| `--config` / `--overlay` | 設定檔，預設 `configs/default.yaml` + `configs/profiles/ota_2p45ghz.yaml`；`--overlay` 可重複 |
| `--transport {udp,uhd,pluto}` | 預設 `udp`（模擬）；`uhd` 是 USRP，`pluto` 是 ADALM-Pluto |
| `--mcs {qpsk,qam16}` | 舊式的 TX modulation 預選；同時給 `--mcs-index` 時後者優先。RX 僅作 display hint，不參與 decode |
| `--mcs-index {0..7}` | 完整的 TX MCS 預選，優先於 `--mcs`；RX 僅作 display hint |
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
| `--enable-rf` / `--acknowledgement` | RF 發射授權，`--transport uhd` 與 `--transport pluto` 必要。`--acknowledgement` 後面的確認文字是 `I acknowledge that this process will transmit RF`，必須一字不差 |

### 只有接收端

| 選項 | 說明 |
|---|---|
| `--egress-host` / `--egress-port` | 轉發已交付 message 的目的地，預設 `127.0.0.1:52002` |
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
| `phy` | Sample rate、center frequency、預設 MCS 與 FEC、synchronization 的門檻 |
| `radio` | `adapter`（`uhd` 才能用 `--transport uhd`）、預設 gain 與 antenna、clock source |
| `acceleration` | `cpu_cores`：限制 NumPy 的 thread 數 |

`simulation` 區段是 PHY 測試用的 channel 模型參數。`node`、`network`、`mac` 區段是設定 schema
要求的欄位，但單向 link 沒有 IP 介面、TDD 或 ARQ，所以用不到它們的值。

## 7.3 MCS 表

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

- 表定義在 [`ofdm_link/phy/mcs_table.py`](../src/ofdm_link/phy/mcs_table.py)。
- TX 視窗依當前 sample rate 即時計算 airtime，上表只是 5 MS/s 的數字。
- Uncoded 的 3 與 7 只有 CRC 偵錯、沒有糾錯，需要明顯更高的 SNR；選到時 GUI 會顯示警告。
- 在 TX 視窗換 MCS 不必 Stop radio，下一則開始 encode 的 message 生效。同一則 fragmented message
  固定使用同一個 MCS。RX 不需跟著操作。
- **不是 8 種 MCS 都有 OTA 實測。** 全部都有模擬的 round-trip 測試；OTA 的結果見
  [throughput 實驗報告](../experiments/ota_udp_throughput/README.md)。

### 為什麼 RX 不必知道 MCS、但 sample rate 必須一致

Burst header 固定使用 QPSK。它的 12 bytes 帶識別碼（magic）、FEC 種類、payload modulation、length 與 CRC。
每個 bit 重複送 3 次，RX 用多數決解出每個 bit 再檢查 CRC。RX 先解 header，再依其中的 modulation 與 FEC 種類
選 payload demapper 與 FEC decoder。

Sample rate 不在 header 裡。兩端不同，等於對「一個 sample 代表多少時間」有不同解讀，連 preamble
都對不上。

FEC 種類在程式裡叫 wire version，是一個編號：v1 = convolutional r1/2、v2 = Turbo r1/3、v3 = convolutional r1/3、
v4 = uncoded。它和 modulation 一起決定 MCS index。

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
- **FEC 與 header 是一起設計的。** Turbo decoder（C++）、用 pilot 的 channel estimation、burst
  偵測、CFO／sample clock offset 補償，以及受保護的 header 都在同一套格式裡一起設計。
- **PHY 只處理 bytes。** 上層不需要知道 GNU Radio 的 tag 或 PDU。

GNU Radio 在這裡的角色是 **radio 的介面**（UHD source／sink、timed burst）以及 rate-1/2
convolutional code 的 Viterbi decoder。

同理，application 與 PHY 之間用一般的 UDP socket，而不是 gr-network 的 block：任何語言寫的程式
都能接上。

## 7.6 測試涵蓋什麼

```bash
python -m pytest -q                 # 全部
python -m pytest tests/phy -q       # 只測 PHY
python -m pytest tests/app -q       # 只測應用程式
```

全部 headless、無硬體、無 display、無 root。

`tests/phy/`：FEC 的 encode／decode、Turbo code、symbol mapping 與 soft demapping、OFDM 調變、
preamble 偵測、channel estimation、burst header，以及完整 burst 在有 noise、multipath、CFO 的
模擬 channel 下的 round-trip。

`tests/app/`：

- Fragment header、fragmentation 與 reassembly，以及不完整 message 的丟棄。
- Sequence gap、16-bit wraparound、out-of-order 的判定。
- 完整 waveform 往返，以及**連續 noise stream 中的 burst 偵測**（刻意用非對齊的 chunk 餵入）。
- Peak normalization 的不 clipping 保證。
- RF 授權閘門：缺 `--enable-rf` 或確認文字不符時必須拒絕。
- Pluto 與 USRP 的列舉、probe 與設定轉換（用假的裝置物件驅動，不需要硬體）。
- Link light 的每一條規則與邊界值。
- 多 worker 的 payload decode 與單 worker 的結果逐 byte 相同。
- 兩個視窗的建構，以及**用 README 的指令實際啟動 app 的 subprocess 測試**。

修改 PHY 之後，除了跑測試，也請重新產生互動導覽的資料：`python -m guide.build_data`。

回到 [README](../README.md)。

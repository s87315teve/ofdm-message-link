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

Burst header 固定使用 QPSK。它的 12 bytes 帶 magic、wire version、payload modulation 與 length，
再經 3 次 bit repetition 的多數決與 CRC 保護。RX 先解 header，再依其中的 modulation 與 wire version
選 payload demapper 與 FEC decoder。

Sample rate 不在 header 裡。兩端不同，等於對「一個 sample 代表多少時間」有不同解讀，連 preamble
都對不上。

Wire version 是 FEC 種類的編號：v1 = convolutional r1/2、v2 = Turbo r1/3、v3 = convolutional r1/3、
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
- **FEC 與 header 是一起設計的。** Turbo decoder（C++）、pilot-based channel estimation、burst
  acquisition、CFO／sample clock offset 補償，以及受保護的 header 都在同一套 wire format 裡。
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

## 7.7 雙向 link 的 control 與 feedback wire format

只有雙向的 `node_app` 會送 control frame。`tx_app` 與 `rx_app` 沒有設定 hook 時，送出與接收的內容
都和以前一樣（`tests/app/test_tx_policy.py` 用固定 seed 比對 burst 的 hash）。

**位置。** Node 的每個 v5 burst 的第一個 MPDU 是一個 `FrameKind.CONTROL` frame：`mcs` = 該 burst
的 modulation，`sequence` 來自一個**獨立的** 16-bit control counter，payload 是一個 Link Control PDU
（LCP）。DATA frame 的 sequence 因此保持連續，舊版 `rx_app` 不會看到假的 gap，只把 CONTROL frame
記在 `control_frames`。沒有 data 時，node 送一個只含 CONTROL frame 的 control-only burst（MCS 8）。
兩個 node 用不同的 scrambler seed 範圍：node 1 用 1–63，node 2 用 64–127（`tx_app` 仍用 1–127）。

**LCP header（8 bytes，big-endian）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | magic | u16 | `0x4C43`（"LC"） |
| 2 | version | u8 | 1；其他版本整個 LCP 被忽略 |
| 3 | pdu_type | u8 | 1 LINK_HEADER、2 FEEDBACK、3 STATUS（保留給 ARQ）；不認得的 type 被忽略 |
| 4 | src_node | u8 | 1–254；等於自己的 id 時，整個 PSDU 是自己 TX 的洩漏，丟棄 |
| 5 | dst_node | u8 | 0 = 任何 node |
| 6 | flags | u8 | b0 CONTROL_ONLY、b1 LBT_MODE、b2 TX_QUEUE_NONEMPTY |
| 7 | body_length | u8 | LINK_HEADER 為 0 |

**FEEDBACK body v1（36 bytes 固定部分）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | report_seq | u16 | 每份 report +1；TX 忽略不是較新的值 |
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
| 36 | entries | 6 B 各一 | seq_first u16、mcs u8、snr 0.25 dB u8（`0xFF` = 無）、ok u8、lost u8 |
| … | TLVs | | type u8、len u8、value；0x80–0xFF 給自訂 reporter |

- 計數器是累計值，所以掉了幾份 feedback 也不會遺失資訊：TX 把收到的兩份相減。
- 解碼端遇到任何格式錯誤都回傳 `None`，不會丟例外；body 比已知的長時，多出的部分被忽略。
- 成本：LINK_HEADER 每個 burst 24 bytes（含 delimiter 與 padding）；4 個 entries 的 FEEDBACK
  是 84 bytes。
- Codec 在 [`ofdm_message_link/control.py`](../src/ofdm_message_link/control.py)，report 的資料結構在
  [`ofdm_message_link/la/api.py`](../src/ofdm_message_link/la/api.py)。

## 7.8 `node_app`：雙向 node 的命令列

一個 Pluto 一個 process。Node 同時發送與接收（FDD）：node 1 在 2420 MHz 發、2460 MHz 收，node 2
相反（[`configs/profiles/duplex_fdd_2420_2460.yaml`](../configs/profiles/duplex_fdd_2420_2460.yaml)，
備用 `duplex_fdd_2405_2475.yaml`）。一個 Pluto 只能開一個 libiio context（第二個回 EBUSY），所以 TX 與
RX 共用一個 `pluto.SharedPlutoContext`，在同一個 engine 子 process 裡。

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
| `--overlay` | 預設 `configs/profiles/duplex_fdd_2420_2460.yaml`；必須有 `duplex` 段 |
| `--tx-freq` / `--rx-freq` | 覆寫 profile 的頻率；信號邊緣離開 2400–2483.5 MHz 時警告 |
| `--tx-gain` / `--rx-gain` | 預設 −89.75 / 15 dB。`--gain` 會被拒絕（node 有兩個 gain） |
| `--rx-analog-bw HZ` | AD9361 RX analog filter，預設 = sample rate |
| `--rx-kernel-buffers N` | libiio RX kernel buffers，預設 4 |
| `--tx-duty-cap D` | `pluto`：TX duty 不超過 D，預設 0.7（USB 2.0 同時帶 5 MS/s RX 時的上限，見 M0）；1 = 不限 |
| `--headless` | 不開視窗，隱含 `--auto-start`；`pluto` 需要 `--serial` |
| `--duration S` | Ready 後 S 秒停止（headless） |
| `--gain-profile "T:DB,..."` | 依 ready 後的秒數排程 TX gain，例如 `0:-5,30:-20,60:-5` |
| `--control-port P` | localhost UDP JSON control socket，預設依 node；0 = 關閉 |
| `--link-adaptation SPEC` | `fixed`（預設，固定在 `--mcs-index`）、`olla`、`arf`、`pkg.mod:Class`、`file.py:Class` |
| `--link-adaptation-arg K=V` | LA class 的參數，可重複 |
| `--feedback-reporter SPEC` / `--feedback-reporter-arg K=V` | RX 端量測與回報模組，預設 `default` |
| `--mcs-index N` | 固定或起始的 MCS，預設 8 |
| `--control-mcs-index N` | Control-only burst 的 MCS，預設 profile 的值（8） |
| `--decode-workers N` | 預設 3（兩個 node 在同一台 PC 時的 CPU 預算） |
| `--stats-log PATH` | 每 0.5 s 一行 JSON：`tx`、`rx`、`la`、`feedback`、`cpu`；control socket 的 `mark` 也寫入 |
| `--la-log PATH` | 每個 LA decision 與 feedback report 一行 JSON |
| `--ready-file PATH` | 兩個方向都在跑時建立，停止時刪除 |
| `--ingress-port` / `--egress-host` / `--egress-port` | 覆寫依 node 的預設 |
| `--udp-listen-port` / `--udp-peer-port` | `--transport udp` 的 sample ports，預設自己與 peer 的 base+4 |
| `--enable-rf` / `--acknowledgement` | `--transport pluto` 必要，和 `tx_app` 相同 |

**Control socket**：每個 datagram 一個 JSON request，回一個 JSON answer。

| Request | 動作 |
|---|---|
| `{"cmd":"set_gain","dir":"tx","db":-20}` | 立即改 gain，回 `{"ok":true,"readback":-20.0}` |
| `{"cmd":"set_mcs","mcs":12}` | 固定 MCS；`"mcs":null` 交回 LA |
| `{"cmd":"set_la","mode":"auto"}` | `auto` 或 `fixed`（凍結目前的 MCS） |
| `{"cmd":"mark","text":"phase 2"}` | 同時寫入 stats log 與 LA log |
| `{"cmd":"stats"}` / `{"cmd":"stop"}` | 最近一行 stats／停止 |

Headless 收到 SIGTERM／SIGINT 或 `--duration` 到時：先停 TX（attenuator 回到最小輸出），再停 RX，
刪除 ready file，exit 0；radio 啟動失敗時 exit 2，engine 子 process 中途結束時 exit 3。

**USB 限制（M0，`experiments/duplex_fdd/results/usb/`）**：Pluto 同時以 5 MS/s 接收時，TX duty 到
0.7 都沒有 overflow；要求 0.8 以上時 TX 只到 0.78，其中一個 Pluto 每分鐘 overflow 約 180–220 次。
8 個 kernel buffers 沒有改善。

回到 [README](../README.md)。

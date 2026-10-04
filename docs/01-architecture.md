# 1. 系統架構

這篇先說明幾個名詞各自在解決什麼問題，再說明這個專案怎麼把它們接起來。
還不熟 dB、I/Q、constellation 或 FFT 的話，請先讀[先備觀念與名詞表](00-prerequisites.md)。
想邊看邊點的話，請開[互動導覽](https://s87315teve.github.io/ofdm-message-link/guide/)。

## 1.1 每一塊在解決什麼問題

一條無線 link 要做的事只有一件：**把一段 bytes 從 A 送到 B，而且 B 要能確定收到的是對的**。
下面每個名詞都是為了解決途中的一個具體問題。數字都以預設的 5 MS/s 計算（一個 sample 是 0.2 µs）。

### Modulation（QPSK、16QAM）

**問題：** Radio 只能送連續的波形，不能直接送 0 和 1。

**做法：** 把 2 個 bits（QPSK）或 4 個 bits（16QAM）對應到複數平面上的一個點，叫 constellation symbol。
點越密，一次送的 bits 越多，但也越容易被 noise 推到隔壁的點。

### OFDM

**問題：** 訊號經過多條路徑（multipath）到達，晚到的回音會蓋到下一個 symbol 上。
如果一個 symbol 很短，回音可能蓋住好幾個 symbol。

**做法：** 把 5 MHz 的頻寬切成 64 個 subcarrier，每個寬 5 MHz ÷ 64 = 78.125 kHz。
每個 subcarrier 各自送自己的 constellation symbol，速度慢很多：一個 OFDM symbol 長 12.8 µs（64 個 samples）。
Symbol 變長了，同樣長度的回音相對就變短。TX 用 IFFT 一次產生全部 subcarrier，RX 用 FFT 把它們分開。

### Cyclic prefix（CP）

**問題：** 即使 symbol 變長，回音還是會落進下一個 symbol 的開頭。

**做法：** 每個 OFDM symbol 前面先重複一次自己最後的 16 個 samples（3.2 µs）。回音只會落在這段 CP 裡，
RX 直接丟掉它。只要回音晚到的時間少於 3.2 µs（路徑差大約 1 km），就不會影響資料。
加上 CP 後，一個 OFDM symbol 是 80 個 samples，也就是 16 µs。

### Preamble

**問題：** RX 不知道 burst 什麼時候開始。兩台 radio 的中心頻率也有一點差（CFO）。

**做法：** 每個 burst 開頭放一段 RX 事先知道的訊號。它的前後兩半完全相同，各 32 個 samples（6.4 µs）。

- **找起點：** RX 一直比較「目前這 32 個 samples」和「再往後 32 個 samples」。兩段幾乎一樣的位置就是 burst 的起點。
- **算 CFO：** 如果兩台 radio 的頻率差 Δf，後半會比前半多轉一個角度 φ = 2π · Δf · 6.4 µs。
  RX 量出 φ，就能算出 Δf = φ ÷ (2π · 6.4 µs)。φ 只能在 ±π 之間，所以這個方法能量到的 Δf 最多 ±78 kHz，
  剛好是一個 subcarrier 的寬度。

這個方法叫 Schmidl–Cox 方法。

### Training symbol 與 pilot

**問題：** Channel 會改變每個 subcarrier 的振幅與相位，而且每個 subcarrier 改變的量不一樣。

**做法：**

1. Preamble 後面是一個內容已知的 OFDM symbol，叫 training symbol。RX 用它算出每個 subcarrier
   被改變了多少（channel estimation），之後每個 symbol 都除回來（equalization）。
2. 之後每個 OFDM symbol 裡還有 4 個內容已知的 subcarrier，叫 pilot。RX 用它們追蹤慢慢漂移的相位。

### FEC（Turbo、convolutional code）

**問題：** Noise 會讓某些 bits 判斷錯誤。

**做法：** 送出去之前加上多餘的 bits。Code rate 1/3 表示每 1 個資料 bit 送出 3 個 bits。
RX 利用多餘的 bits 把錯的 bits 修回來。代價是速率變成三分之一。

### CRC

**問題：** FEC 修不回來時，RX 需要知道「這個 frame 是壞的」。

**做法：** 每個 frame 尾端加上 32-bit 的檢查碼。RX 重新算一次，結果不一樣就整個丟掉，絕不交付錯的資料。
CRC 就像身分證字號的最後一碼：它能發現錯誤，但不能修正錯誤。修正是 FEC 的工作。

### MCS

**問題：** SNR 高的時候想送快一點，SNR 低的時候想送穩一點。

**做法：** 把 modulation 和 FEC 的組合編號。MCS 0（QPSK、Turbo 1/3）最穩最慢，
MCS 7（16QAM、沒有 FEC）最快，但需要很高的 SNR。

### Burst

**問題：** 資料不是連續不斷的，RX 需要知道一段資料的邊界。

**做法：** 一個 frame 加上 preamble、training 與 header 之後，變成時間上獨立的一小段訊號，前後是靜音（guard）。

---

一個觀念貫穿全部：**速率和可靠度是交換來的。** 點放得密（16QAM）、多餘的 bits 加得少（rate 1/2 或不加），
同一段時間送的 bits 就多，但需要的 SNR 也高。這個專案讓你在 TX 視窗用一個選單切換這個取捨，
並在 RX 視窗直接看到後果。

## 1.2 一則 message 的旅程

一段資料從應用程式到 radio，會經過五種單位。用一則 2000 bytes 的 message 當例子：

| 層 | 單位 | 做了什麼 | 例子 |
|---|---|---|---|
| Application | **message** | 應用程式送出一個 UDP datagram，就是一則 message | 2000 B |
| Link | **fragment** | 切成每段最多 968 B 的資料，每段加上 28 B 的 fragment header（reassembly 用） | 968＋28、968＋28、64＋28 B |
| Link | **PHY frame** | 每個 fragment 加上 7 B 的 frame header（含 sequence number）與 4 B 的 CRC | 1007、1007、103 B |
| PHY | **burst** | 每個 PHY frame 經過 FEC、modulation 與 OFDM，變成一段 complex samples | 3 個 burst |
| Channel | samples | 3 個 burst 依序送出 | — |

接收端反過來做。**3 個 burst 裡只要遺失 1 個，整則 message 就會被丟掉**，因為單向 link 沒辦法要求重送。
所以對遺失敏感的資料，每則 message 最好小於 968 B，只用一個 burst。

## 1.3 三層架構

一段 bytes 從左上進、左下出，中間經過三層。**應用程式只認得 bytes 和 UDP socket**，
完全不知道底下的 MCS、FEC 或是否有 radio：

![System block diagram：上排 TX chain，右邊 channel，下排 RX chain](images/ofdm-message-link-architecture.png)

| 層 | 發送端（上排，左 → 右） | 接收端（下排，右 → 左） | 程式 |
|---|---|---|---|
| Application | Ingress（入口）：GUI input box 或 UDP `:52001` | Egress（出口）：GUI message list 與 UDP `:52002` | `tx_app.py`、`rx_app.py` |
| Link | Fragmentation：大於 968 B 就切開，每個 fragment 加 28 B header | Reassembly：依 `message_id` 組回，缺 fragment 就整則丟 | `datagram.py` |
| | PHY frame：16-bit sequence number + CRC-32 | Frame check：CRC、sequence number 跳號 = 遺失 | `link.py` |
| PHY | FEC encoder → symbol mapper → OFDM modulator → burst assembly | Burst 偵測 → header decode → equalizer → demapper → FEC decoder | `ofdm_link.phy` |
| Sample transport | Peak normalize → UDP sink、UHD sink 或 Pluto sink | UDP source、UHD source 或 Pluto source | `transport.py`、`pluto.py` |

**Sample transport** 是把 samples 從 TX 程式搬到 RX 程式的那一層。模擬與實機**只有 sample transport
與 channel 不同**，其餘全是同一份程式碼、同一套 GUI、同一組量測。
這就是為什麼你可以先在沒有硬體的情況下把整條 link 弄懂。

## 1.4 一個 burst 長什麼樣子

每個 PHY frame 變成一個獨立的 burst（數字是 samples，括號內是 5 MS/s 時的時間）：

```
┌─────────┬──────────┬──────────┬────────────────┬───────────────────────────┬─────────┐
│  guard  │ preamble │ training │     header     │          payload          │  guard  │
│   256   │    80    │    80    │ 240 = 3 OFDM   │ N OFDM symbols × 80       │   256   │
│(51.2 µs)│ (16 µs)  │ (16 µs)  │ symbols (48 µs)│ (MCS 0、968 B：N = 255)   │(51.2 µs)│
└─────────┴──────────┴──────────┴────────────────┴───────────────────────────┴─────────┘
             burst 偵測  channel     MCS、length
                        estimation
```

- 每個 OFDM symbol 是 80 個 samples：64 個（FFT 大小）加上 16 個 cyclic prefix。
- 64 個 subcarrier 裡用了 52 個：48 個放資料、4 個是 pilot（第 −21、−7、+7、+21 個）。
  中間的 DC subcarrier 和兩側邊緣留空。
- 以 MCS 0、968 B 的 fragment 為例，整個 burst 是 21,312 個 samples，大約 4.26 ms。

**Header 怎麼保護：** Header 有 12 bytes（96 bits），包含識別碼、payload 的 modulation、FEC 種類、length 與 CRC。
每個 bit 重複送 3 次，變成 288 bits；固定用 QPSK，每個 OFDM symbol 帶 48 × 2 = 96 bits，
所以剛好佔 3 個 OFDM symbols。RX 對每個 bit 做多數決，再檢查 CRC。
因此 header 比 payload 耐得住 noise。

**接收端先解 header 才知道 payload 用哪種 MCS，所以 RX 不必預先設定 MCS。**
可以這樣想：header 像信封上寫著「內容用哪種語言」，RX 讀了信封就知道怎麼讀內容。

但 sample rate 不在 header 裡。Sample rate 決定「一個 sample 代表多少時間」，就像「多快唸一個字」。
兩端不同，連 preamble 都對不上，更讀不到信封。所以兩端**必須一致的只有 sample rate**。

## 1.5 Process 怎麼分

兩個 app 預設 `--engine process`：

- Qt 視窗在主 process。
- Radio、encode／decode 與 UDP 入出口在子 process，所以 GUI 繪圖不會卡住 sample path。
- RX 另外預設 `--decode-workers 4`，把 payload decode 分到 4 個 process。Burst 偵測與
  header 仍由單一 owner 依序處理，交付順序不變。

為什麼用 process 而不是 thread：Python 的 interpreter lock 讓多個 thread 無法同時跑 Python 程式碼，
decode 分到 thread 上幾乎不會變快；分到 process 才能真的用到多個 CPU 核心。

## 1.6 檔案對照

應用程式，在 `src/ofdm_message_link/`：

| 檔案 | 負責 |
|---|---|
| `tx_app.py` / `rx_app.py` | 兩個視窗、UDP ingress / egress、發送與接收 thread |
| `datagram.py` | 28 B fragment header、fragmentation、reassembly、丟棄不完整的 message |
| `link.py` | bytes ⇄ burst 的兩半；sequence gap、EVM/SNR 量測 |
| `transport.py` | UDP 與 UHD 兩種 sample transport、peak normalization、TX 定時發射 |
| `pluto.py` | ADALM-Pluto：經 libiio 列舉、probe，以及 Pluto 的 sample sink／source |
| `engine.py` | 把 radio 路徑放進子 process |
| `options.py` | 兩端共用的命令列參數、設定檔解析、RF 授權檢查 |
| `devices.py` / `radio_panel.py` | 列舉與 probe radio、Radio panel |
| `dashboard.py` | Link light 規則與滑動視窗統計（不 import Qt，可 headless 測試） |
| `dashboard_widgets.py` / `plots.py` | Cards、trend、constellation、spectrum |
| `qt_runtime.py` / `window_layout.py` | Qt 啟動、Ctrl-C、`--geometry` 與影像 demo 視窗排列 |
| `log_limits.py` | log 大小上限與輪替、清舊 run 目錄 |
| `udp_send.py` / `udp_recv.py` | 最小的 socket 生產者／消費者範例 |

PHY library，在 `src/ofdm_link/`：

| 檔案 | 負責 |
|---|---|
| `phy/codec.py` | Frame 的格式與 CRC；symbol mapping（QPSK、16QAM） |
| `phy/fec.py` / `phy/turbo.py` / `phy/turbo_native.cpp` | Convolutional code 與 Turbo code；C++ 寫的 decoder |
| `phy/ofdm.py` | Subcarrier 配置、IFFT／FFT、cyclic prefix、pilot |
| `phy/sync.py` | Preamble 的產生與偵測、CFO 估計 |
| `phy/channel.py` | Channel estimation 與 equalization |
| `phy/burst_header.py` | Burst header 的編碼與保護 |
| `phy/burst.py` | 把上面全部組成一個 burst，以及反過來解一個 burst |
| `phy/streaming.py` | 從連續的 sample stream 裡找出一個個 burst |
| `phy/mcs_table.py` | 8 種 MCS 的定義 |
| `radio/uhd.py` | 建立 UHD（USRP）的 source 與 sink，以及 RF 授權閘門 |
| `radio/simulation.py` | 測試用的 channel 模型（multipath、CFO、sample clock offset） |
| `runtime/factory.py` | 依設定挑選成對的 encoder／decoder |
| `runtime/segment_decode_pool.py` | 多 process 的 payload decode |
| `config.py` | 讀取並驗證 YAML 設定檔 |

**建議的閱讀順序**：`link.py`（最短，看得到整條路徑）→ `phy/burst.py` 的 `encode_burst` 與
`decode_burst` → 你有興趣的那一層。

## 1.7 這個範例刻意不做的事

- **TCP**：TCP 的 handshake 與 ACK 需要反向路徑，單向 link 做不起來。
- **雙向 / ARQ / TDD**：需要 MAC layer 來排程誰在什麼時候發射，這裡沒有。
- **完整的 channel model**：`--snr-db` 只加 AWGN，沒有 fading、CFO 或 sample clock offset。
  （`ofdm_link.radio.simulation` 有比較完整的模型，測試會用到，但 GUI 沒有接上它。）

下一篇：[看懂 GUI](02-gui.md)。

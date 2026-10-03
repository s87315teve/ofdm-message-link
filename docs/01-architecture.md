# 1. 系統架構

這篇先用很短的篇幅說明幾個名詞各自在解決什麼問題，再說明這個專案怎麼把它們接起來。
想邊看邊點的話，請開[互動導覽](https://s87315teve.github.io/ofdm-message-link/guide/)。

## 1.1 先備觀念：每一塊在解決什麼問題

一條無線 link 要做的事只有一件：**把一段 bytes 從 A 送到 B，而且 B 要能確定收到的是對的**。
下面每個名詞都是為了解決途中的一個具體問題。

| 名詞 | 要解決的問題 | 這個專案的做法 |
|---|---|---|
| **Modulation**（QPSK、16QAM） | Radio 只能送連續的波形，不能直接送 0 和 1 | 把 2 個 bit（QPSK）或 4 個 bit（16QAM）對應到複數平面上的一個點，叫 symbol。點越密，一次送越多 bit，但也越容易被 noise 推到隔壁的點 |
| **OFDM** | 訊號經過多條路徑（multipath）到達時，前一個 symbol 的回音會蓋到下一個 symbol | 把頻寬切成 64 個很窄的 subcarrier，每個 subcarrier 各自慢慢送自己的 symbol。symbol 變長了，回音相對就短。TX 用 IFFT 一次產生全部 subcarrier，RX 用 FFT 分開 |
| **Cyclic prefix（CP）** | 即使 symbol 變長，回音還是會落進下一個 symbol 的開頭 | 每個 OFDM symbol 前面先重複自己最後 16 個 sample。回音只會落在這段 CP 裡，RX 直接丟掉它 |
| **Preamble** | RX 不知道 burst 什麼時候開始，兩台 radio 的頻率也有一點差（CFO） | 每個 burst 開頭放一段 RX 事先知道的訊號，前後兩半完全相同。RX 找「前後兩半一樣」的地方就是起點，兩半的相位差就是頻率偏移（Schmidl–Cox 方法） |
| **Training symbol 與 pilot** | Channel 會改變每個 subcarrier 的振幅與相位 | Training symbol 是整個已知的 OFDM symbol，RX 用它算出每個 subcarrier 被改變了多少（channel estimation），再除回來（equalization）。之後每個 symbol 裡還有 4 個已知的 pilot subcarrier，用來追蹤慢慢漂移的相位 |
| **FEC**（Turbo、convolutional code） | Noise 會讓某些 bit 判斷錯誤 | 送出去之前加上冗餘的 bit。Code rate 1/3 表示每 1 個資料 bit 送 3 個 bit。RX 利用冗餘把錯的 bit 修回來。代價是速率變成三分之一 |
| **CRC** | FEC 修不回來時，RX 需要知道「這包是壞的」 | 每個 frame 尾端加上 32-bit 的檢查碼。算出來不一樣就整包丟掉，絕不交付錯的資料 |
| **MCS** | SNR 高的時候想送快一點，SNR 低的時候想送穩一點 | 把 modulation 和 FEC 的組合編號。MCS 0（QPSK + Turbo 1/3）最穩最慢，MCS 7（16QAM、沒有 FEC）最快但需要很高的 SNR |
| **Burst** | 資料不是連續不斷的，RX 需要知道一包的邊界 | 一包資料加上 preamble、training、header 之後，在時間上是獨立的一小段訊號，前後是靜音 |

一個觀念貫穿全部：**速率和可靠度是交換來的。** 點放得密（16QAM）、冗餘加得少（rate 1/2 或不加），
同一段時間送的 bit 就多，但需要的 SNR 也高。這個專案讓你在 TX 視窗用一個選單切換這個取捨，
並在 RX 視窗直接看到後果。

## 1.2 三層架構

一段 bytes 從左上進、左下出，中間經過三層。**應用程式只認得 bytes 和 UDP socket**，
完全不知道底下的 MCS、FEC 或是否有 radio：

![System block diagram：上排 TX chain，右邊 channel，下排 RX chain](images/ofdm-message-link-architecture.png)

| 層 | 發送端（上排，左 → 右） | 接收端（下排，右 → 左） | 程式 |
|---|---|---|---|
| Application | Ingress：GUI input box 或 UDP `:52001` | Egress：GUI message list 與 UDP `:52002` | `tx_app.py`、`rx_app.py` |
| Link | Datagram：加 28 B header，大於 968 B 就 fragment | Reassembly：依 `message_id` 組回，缺 fragment 就整則丟 | `datagram.py` |
| | PHY frame：16-bit sequence number + CRC-32 | Frame check：CRC、sequence gap = loss | `link.py` |
| PHY | FEC encoder → symbol mapper → OFDM modulator → burst assembly | Acquisition → header decode → equalizer → demapper → FEC decoder | `ofdm_link.phy` |
| Sample transport | Peak normalize → UDP sink、UHD sink 或 Pluto sink | UDP source、UHD source 或 Pluto source | `transport.py`、`pluto.py` |

模擬與實機**只有 sample transport 與 channel 不同**，其餘全是同一份程式碼、同一套 GUI、同一組量測。
這就是為什麼你可以先在沒有硬體的情況下把整條 link 弄懂。

## 1.3 一個 burst 長什麼樣子

每則 datagram 變成一個獨立的 burst：

```
┌───────┬──────────┬──────────┬──────────────┬──────────────────────────┬───────┐
│ guard │ preamble │ training │ header × 3   │ payload × N OFDM symbols │ guard │
│  256  │    80    │    80    │ QPSK, fixed  │ N set by MCS (MCS 0: 255)│  256  │
└───────┴──────────┴──────────┴──────────────┴──────────────────────────┴───────┘
          acquisition  channel    MCS, length                        (unit: samples)
                       estimate
```

- 每個 OFDM symbol 是 80 個 sample：64 個（FFT 大小）加上 16 個 cyclic prefix。
- 64 個 subcarrier 裡用了 52 個：48 個放資料、4 個是 pilot（第 −21、−7、+7、+21 個），
  中間的 DC 和兩側邊緣留空。
- Header 固定用 QPSK，並重複三次做多數決，所以它比 payload 耐得住 noise。

**接收端先解 header 才知道 payload 用哪種 MCS，所以 RX 不必預先設定 MCS。** 但 sample rate 不在
header 裡：兩端 sample rate 不同，等於對「一個 sample 代表多少時間」有不同解讀，連 preamble 都
對不上。所以兩端**必須一致的只有 sample rate**。

## 1.4 Process 怎麼分

兩個 app 預設 `--engine process`：

- Qt 視窗在主 process。
- Radio、encode／decode 與 UDP 入出口在子 process，所以 GUI 繪圖不會卡住 sample path。
- RX 另外預設 `--decode-workers 4`，把 payload decode 分到 4 個 process。Preamble acquisition 與
  header 仍由單一 owner 依序處理，交付順序不變。

為什麼用 process 而不是 thread：Python 的 interpreter lock 讓多個 thread 無法同時跑 Python 程式碼，
decode 分到 thread 上幾乎不會變快；分到 process 才能真的用到多個 CPU 核心。

## 1.5 檔案對照

應用程式，在 `src/ofdm_message_link/`：

| 檔案 | 負責 |
|---|---|
| `tx_app.py` / `rx_app.py` | 兩個視窗、UDP ingress / egress、發送與接收 thread |
| `datagram.py` | 28 B header、fragmentation、reassembly、不完整訊息丟棄 |
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

## 1.6 這個範例刻意不做的事

- **TCP**：TCP 的 handshake 與 ACK 需要反向路徑，單向 link 做不起來。
- **雙向 / ARQ / TDD**：需要 MAC layer 來排程誰在什麼時候發射，這裡沒有。
- **完整的 channel model**：`--snr-db` 只加 AWGN，沒有 fading、CFO 或 sample clock offset。
  （`ofdm_link.radio.simulation` 有比較完整的模型，測試會用到，但 GUI 沒有接上它。）

下一篇：[看懂 GUI](02-gui.md)。

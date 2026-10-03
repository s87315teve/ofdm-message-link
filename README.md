# ofdm-message-link：單向 OFDM message link

在一個視窗打字，在另一個視窗看到它從 OFDM waveform 裡被解出來。

這是一個用 GNU Radio 與 Python 寫的**教學用通訊系統範例**。它把一段 bytes 變成 OFDM burst、送過
channel、再解回同一段 bytes，並且把過程中的 spectrum、constellation、SNR 與 packet loss 即時畫出來。

- **沒有硬體也能跑**：`--transport udp` 用本機 UDP 模擬 channel，不需要 SDR、root 或 GPU。
- **有 SDR 就能真的發射**：`--transport uhd` 支援 USRP B210、NI USRP-2901 與 USRP N210；
  `--transport pluto` 支援 ADALM-Pluto。
- **任何程式都能接上**：發送端收 UDP `:52001`、接收端送 UDP `:52002`，你的程式只要會開 socket。
- **可以自己加 SDR**：sample transport 是一個小介面，見 [新增一種 SDR](docs/05-add-a-new-sdr.md)。

TX 和 RX 是**兩個各自啟動的 process**，彼此只透過 sample transport 溝通，所以接收端顯示的內容
一定是真的從 waveform 解出來的。

[![System block diagram：上排 TX chain，右邊 channel，下排 RX chain](docs/images/ofdm-message-link-architecture.png)](https://s87315teve.github.io/ofdm-message-link/guide/)

> 👉 **互動導覽：<https://s87315teve.github.io/ofdm-message-link/guide/>**
> 點任何一個模組，看它做什麼、對應哪一段程式、在 GUI 的哪裡看得到；也可以調 message 大小與 MCS，
> 看 packet 怎麼一層層包起來。離線時用瀏覽器開 [`guide/index.html`](guide/index.html) 也一樣。

## 這個範例是什麼、不是什麼

這是**單向** link：只有 TX → RX，沒有回傳路徑。所以：

- **沒有 ACK、沒有重傳（ARQ）、沒有 TDD**。掉的 packet 就是掉了，不會自己回來。
- 這讓你可以直接觀察「channel 變差時會發生什麼事」，不會被重傳機制蓋掉。
- 畫面上的 throughput、loss、SNR 是觀察用的即時讀數，不是正式的系統效能量測。

兩個視窗最上方都會一直標示這一點。

## 安裝

需要 Linux（在 Ubuntu 上開發與測試）與 [conda](https://docs.conda.io/)（Miniconda 或 Anaconda 都可以）。

```bash
git clone https://github.com/s87315teve/ofdm-message-link.git
cd ofdm-message-link

conda env create -f environment.yml      # 第一次約需數分鐘；GNU Radio、UHD、libiio、PyQt5 都在裡面
conda activate ofdm-message-link
pip install -e .                         # 會編譯一個 C++ 的 Turbo decoder

python -m pytest -q                      # 選用：確認安裝正確，全部應該通過
```

之後每開一個新的終端機，都要先 `cd` 到這個資料夾並 `conda activate ofdm-message-link`。
**所有指令都從 repository root 執行**（程式用相對路徑讀 `configs/`）。

## 五分鐘上手（不需要硬體）

開**兩個終端機**。

**① 終端機 1：接收端**（先開，它要先佔住 sample transport 的 port）：

```bash
python -m ofdm_message_link.rx_app --snr-db 15
```

**② 終端機 2：發送端**：

```bash
python -m ofdm_message_link.tx_app
```

**③ 在發送端視窗的輸入框打字、按 Enter。** 接收端視窗會列出這則訊息、更新 constellation 與 spectrum，
並累加統計。

`--snr-db 15` 讓接收端加上 AWGN，constellation 才看得出 noise 擴散；拿掉它就是無 noise 的乾淨路徑。
想試 16QAM（需要較高 SNR）：

```bash
python -m ofdm_message_link.rx_app --snr-db 24
python -m ofdm_message_link.tx_app --mcs qam16
```

以下是 QPSK／15 dB 指令的實際畫面（連續送出 44 則文字訊息時截圖，44 則全部送達）：

| 發送端 | 接收端 |
|---|---|
| ![Transmitter window: top cards with an OK light and SIMULATED badge, the 60-second goodput curve and the Log tab listing sent messages](docs/images/ofdm-message-link-tx.png) | ![Receiver window: GOOD light at 12.5 dB SNR with 0.00 % loss, the 60-second goodput and SNR curve and the Signal tab with a four-cluster QPSK constellation](docs/images/ofdm-message-link-rx.png) |

**可以動手試的三件事：**

1. 把 `--snr-db` 從 15 慢慢降到 5，看 constellation 怎麼散開、`Link` light 什麼時候變色。
2. 在 TX 視窗的 `MCS for next message` 選單換成 MCS 7（16QAM、沒有 FEC），看同樣的 SNR 下還收不收得到。
3. 在 TX 的 `Send` tab 用 **Loss test** 一次送 100 則，讀 RX 的 `Loss (10 s)`。這就是 packet error rate。

## 接下來讀什麼

建議照順序讀。前三篇不需要硬體。

| # | 文件 | 內容 |
|---|---|---|
| 1 | [系統架構](docs/01-architecture.md) | OFDM、FEC、MCS 各自解決什麼問題；每個 block 做什麼、在哪個檔案 |
| 2 | [看懂 GUI](docs/02-gui.md) | Spectrum 與 constellation 怎麼讀、怎麼量 packet loss、每個統計數字的意義與限制 |
| 3 | [接上你自己的程式](docs/03-your-own-app.md) | 用 UDP socket 收送資料；webcam 影像串流 demo |
| 4 | [用真的 SDR 發射](docs/04-ota-hardware.md) | **發射前必讀的注意事項**；USRP 與 ADALM-Pluto 的設定 |
| 5 | [新增一種 SDR](docs/05-add-a-new-sdr.md) | Sample transport 的介面，以及加一種新裝置要改哪幾個地方 |
| 6 | [收不到東西？](docs/06-troubleshooting.md) | 照順序檢查的排錯清單 |
| 7 | [參考資料](docs/07-reference.md) | 全部的命令列參數、MCS 表、設計上的技術細節 |
| 8 | [實測紀錄](docs/08-measurements.md) | 實機 OTA 的量測結果，以及一份完整的 [throughput 實驗報告](experiments/ota_udp_throughput/README.md) |

## 專案結構

```
ofdm-message-link/
├── src/
│   ├── ofdm_message_link/   應用程式：兩個視窗、datagram、sample transport、GUI
│   └── ofdm_link/           PHY library：FEC、OFDM、synchronization、equalization，以及 USRP 介面
├── configs/                 設定檔：default.yaml 加上 profiles/ 裡的一個 profile
├── docs/                    上表的文件
├── guide/                   互動導覽（純 HTML/JS，不需要 server）
├── experiments/             量測腳本與報告
├── scripts/                 webcam 影像 demo 的一鍵腳本
└── tests/                   測試：全部不需要硬體、display 或 root
```

兩個 package 的分工：**`ofdm_message_link` 只處理 bytes、視窗與 radio 的選擇；`ofdm_link.phy` 才是
把 bytes 變成 waveform 的地方。** 想研究 OFDM 或 FEC 的實作，讀 `src/ofdm_link/phy/`；想改 GUI
或加新的 SDR，讀 `src/ofdm_message_link/`。

## 測試

```bash
python -m pytest -q
```

全部 headless：不需要 SDR、display 或 root。修改程式之後先跑一次，可以馬上知道有沒有弄壞既有行為。
`tests/phy/` 測 PHY 的每一層，`tests/app/` 測應用程式（包含用上面的指令實際啟動兩個 app）。

## License

[GPL-3.0-or-later](LICENSE)。

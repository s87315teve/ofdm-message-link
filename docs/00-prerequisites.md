# 0. 先備觀念與名詞表

這篇寫給修過訊號與系統、還沒修過通訊系統的讀者。讀完這篇，再讀[系統架構](01-architecture.md)。
已經學過數位通訊的話，可以直接跳到 [0.6 名詞表](#06-名詞表)。

## 0.1 dB 與 SNR

**dB 是功率比的對數。** 兩個功率 P₁、P₂ 的比是 `10·log10(P₁/P₂)` dB。

| 功率比 | dB |
|---:|---:|
| 2 倍 | 3 dB |
| 10 倍 | 10 dB |
| 100 倍 | 20 dB |
| 1/10 | −10 dB |

**SNR（signal-to-noise ratio）** 是訊號功率除以 noise 功率，通常用 dB 表示。SNR 20 dB 表示訊號功率是
noise 的 100 倍。SNR 越高，接收端越容易判斷送的是什麼。

**dBFS（dB relative to full scale）** 是相對於「ADC 能表示的最大值」的 dB。0 dBFS 是最大值，
−60 dBFS 表示功率是最大值的百萬分之一。GUI 的 `rx level` 用這個單位。

**AWGN（additive white Gaussian noise）** 是最基本的 noise 模型：每個 sample 加上一個獨立的、
平均為 0 的 Gaussian 亂數。模擬模式的 `--snr-db` 就是加這種 noise。

## 0.2 為什麼 sample 是複數（I/Q）

Radio 在 2.45 GHz 發射，可是電腦不需要處理 2.45 GHz 的波形。Radio 的硬體先把訊號移到 0 Hz 附近
（叫 **baseband**），再用兩個 ADC 取樣：

- 一個 ADC 量訊號和 cos 相乘後的結果，叫 **I**（in-phase）。
- 另一個 ADC 量訊號和 sin 相乘後的結果，叫 **Q**（quadrature）。

把兩個數合成一個複數 `I + jQ`，一個 sample 就是一個複數。複數的大小是振幅，角度是相位。
這樣一個 sample 同時記錄了振幅與相位，而且能分辨比中心頻率高或低的頻率。

這個專案的 samples 型別是 `complex64`，大小的上限是 1.0（滿刻度，也就是 0 dBFS）。

**Sample rate** 的單位是 **MS/s**（每秒百萬個 samples）。預設的 5 MS/s 表示每 0.2 µs 一個 sample。
因為 sample 是複數，5 MS/s 可以表示 −2.5 MHz 到 +2.5 MHz 這 5 MHz 寬的頻帶。

## 0.3 Modulation 與 constellation

數位資料是 0 和 1，radio 送的是波形。**Modulation** 把幾個 bits 對應到一個複數值。
這個複數值叫 **constellation symbol**，畫在複數平面上的點叫 **constellation point**，
全部的點合起來叫 **constellation**（星座圖）。

| Modulation | 每個 symbol 帶幾個 bits | Constellation |
|---|---:|---|
| QPSK | 2 | 4 個點，在正方形的四個角 |
| 16QAM | 4 | 16 個點，排成 4 × 4 的格子 |

接收端收到的點會被 noise 推離原本的位置。接收端選「最近的那個點」當答案。
16QAM 的點比 QPSK 密，同樣的 noise 比較容易把點推到隔壁的位置，所以 16QAM 需要比較高的 SNR。

RX 視窗的 constellation 圖就是畫接收端收到的點。點越集中在黃色的參考點上，SNR 越高。

## 0.4 FFT 和 OFDM 的關係

你在訊號與系統學過 DFT：N 個時間 samples 經過 DFT，變成 N 個頻率成分（bins）。FFT 是計算 DFT 的
快速方法，IFFT 是反過來的運算。

**OFDM 把每一個 FFT bin 當作一個獨立的小頻道**，叫 **subcarrier**（子載波）：

1. TX 在 64 個 bins 裡各放一個 constellation symbol（有些 bins 留空）。
2. TX 做 64 點 IFFT，得到 64 個時間 samples。這 64 個 samples 叫一個 **OFDM symbol**。
3. RX 收到 64 個 samples 後做 FFT，就能從每個 bin 拿回 TX 放的 symbol。

**注意：「symbol」有兩種意思。**

- **Constellation symbol**：一個點，帶 2 或 4 個 bits。
- **OFDM symbol**：一段時間訊號，裡面同時帶著 48 個 constellation symbols（加上 4 個 pilots），
  長度是 80 個 samples。

這份教學會寫清楚是哪一種。只寫「symbol」的時候，從上下文判斷：講 bits 時是 constellation symbol，
講 samples 或時間時是 OFDM symbol。

## 0.5 Multipath

無線訊號會經過牆壁、桌子反射，所以接收端收到的是很多份「延遲不同、大小不同」的同一個訊號，
叫 **multipath**。晚到的那幾份就像回音，會蓋到下一段訊號上。[系統架構](01-architecture.md)
說明 OFDM 和 cyclic prefix 怎麼處理這個問題。

## 0.6 名詞表

下表的名詞在程式、GUI 和命令列參數裡都用英文，所以這份教學也保留英文。第一欄是這份教學使用的寫法。

### 資料的單位

一段資料從應用程式到 radio，依序是下面五種單位。完整的例子見
[系統架構 1.2 節](01-architecture.md#12-一則-message-的旅程)。

| 名詞 | 意思 |
|---|---|
| **message** | 應用程式交給 link 的一段 bytes，大小不限。一個 UDP datagram 就是一則 message |
| **UDP datagram** | UDP socket 一次送出的資料。這份教學裡「datagram」只指 UDP datagram |
| **fragment** | Message 切開後的一段，最多帶 968 bytes 的資料，前面加 28 bytes 的 **fragment header**（程式裡的 class 叫 `Datagram`，在 `datagram.py`） |
| **PHY frame** | 一個 fragment 加上 7 bytes 的 **frame header** 與 4 bytes 的 CRC，最多 1007 bytes |
| **burst** | 一個 PHY frame 變成的一段 complex samples。前後各有一段 **guard**（256 個值為 0 的 samples） |

這份教學裡，**burst 遺失**、**burst loss**、**packet loss** 與 **PER**（packet error rate）指的都是
同一件事：送出去的 burst 沒有被正確解出來。

### 通訊名詞

| 名詞 | 中文 | 意思 |
|---|---|---|
| subcarrier | 子載波 | OFDM 裡的一個 FFT bin |
| DC subcarrier | — | 0 Hz 的那個 bin。Radio 在 0 Hz 常有漏過來的 carrier 與 DC offset，所以這個 bin 不放資料 |
| cyclic prefix（CP） | 循環前綴 | 每個 OFDM symbol 前面重複一次自己最後的 16 個 samples |
| preamble | 前導訊號 | 每個 burst 開頭，接收端事先知道的一段訊號，用來找到 burst 的起點 |
| training symbol | 訓練符元 | Preamble 後面一個內容已知的 OFDM symbol，用來做 channel estimation |
| pilot | 導頻 | 每個 OFDM symbol 裡 4 個內容已知的 subcarrier，用來追蹤相位的漂移 |
| channel estimation | 通道估測 | 估計 channel 對每個 subcarrier 改變了多少振幅與相位 |
| equalization | 等化 | 把 channel 造成的改變除回來 |
| CFO（carrier frequency offset） | 載波頻率偏移 | 兩台 radio 的中心頻率不會完全相同，兩者的差就是 CFO |
| sample clock offset | 取樣時脈偏移 | 兩台 radio 的取樣速度也有微小差異，時間一長，取樣點會慢慢偏掉 |
| FEC（forward error correction） | 前向錯誤更正 | 送出前加入多餘的 bits，讓接收端能修正錯誤。這個專案有 Turbo 與 convolutional 兩種 |
| code rate | 碼率 | 資料 bits 佔送出 bits 的比例。Rate 1/3 表示每 1 個資料 bit 送出 3 個 bits |
| FEC 種類 | — | Burst header 裡的一個欄位，告訴接收端 payload 用哪一種 FEC。程式裡叫 **wire version** |
| CRC | 循環冗餘檢查 | 附在資料後面的檢查碼，用來發現錯誤，但不能修正錯誤 |
| MCS（modulation and coding scheme） | 調變與編碼方式 | Modulation 加上 FEC 的組合，這個專案有 MCS 0 到 7 |
| soft demapping、LLR | 軟式解調、對數概似比 | 不直接判斷 bit 是 0 或 1，而是算出「偏向 0 或 1 的程度」（LLR），交給 FEC decoder 使用 |
| majority vote | 多數決 | 同一個 bit 送 3 次，3 次裡至少 2 次相同的值就是答案 |
| burst 偵測 | — | 在連續的 samples 裡找到 burst 的起點，同時估計 CFO 與 gain。程式裡叫 acquisition |
| PAPR（peak-to-average power ratio） | 峰均功率比 | 訊號最大的瞬間功率除以平均功率。OFDM 的 PAPR 大約 15 dB |
| EVM（error vector magnitude） | 誤差向量幅度 | 收到的點和參考點之間的距離，相對於點的平均大小。EVM 越小越好 |
| effective SNR | — | 從 EVM 換算出來的 SNR。計算方法見[看懂 GUI 2.6 節](02-gui.md#26-統計數字的意義與限制) |

### 這個專案的名詞

| 名詞 | 意思 |
|---|---|
| sample transport | 把 samples 從 TX 程式搬到 RX 程式的那一層。模擬時是本機 UDP（`--transport udp`），實機時是 USRP 或 Pluto（`--transport uhd`、`--transport pluto`） |
| channel | Samples 從 TX 到 RX 途中經過的東西：模擬時是加上去的 AWGN，實機時是 cable 或空氣 |
| ingress、egress | 入口與出口。TX 從 ingress port（預設 52001）收 message，RX 把 message 送到 egress port（預設 52002） |
| profile、overlay | 設定檔。`configs/default.yaml` 是基本設定，`--overlay` 指定的 profile 疊在上面，改掉部分設定（例如頻率） |
| Link light | RX 視窗右上角的燈號，用一個顏色表示 link 的狀態 |
| goodput | 只算應用程式資料的速率，不含 header 與 FEC 的多餘 bits |
| airtime | Radio 實際在發射的時間比例 |
| decode stages | RX 的 `Decode` tab 裡的一組計數，依序列出每個階段通過的 burst 數，用來找出 burst 卡在哪一步 |
| foreign bursts | CRC 正確、但不是這個程式格式的 burst，表示同一個頻道上有其他程式在發射 |
| RF 確認文字 | 發射前必須用 `--acknowledgement` 輸入的一句英文，見[用真的 SDR 發射](04-ota-hardware.md#41-發射前必讀) |

下一篇：[系統架構](01-architecture.md)。

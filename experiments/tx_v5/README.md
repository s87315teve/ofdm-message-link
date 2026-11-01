# Wire v5：重新設計發送端、模擬與實機 A/B

[強化接收端](../rx_improvement/README.md)（`improve-rx`）只改 RX，wire format 不變。修完之後，
最穩的 MCS 0 在邊緣的遺失大多是 **preamble 沒抓到**，RX 已經沒有什麼可以再改。要再往下走，
只能改 burst 本身。這個資料夾記錄 `tx-v5` 分支怎麼改、改了多少，以及量測用的腳本與數據。

**TX 只送 wire v5；RX 同時收 v5 與 v1–v4。** 舊版 TX 送出的 burst，新 RX 照樣收（錄音重解的結果
與舊 RX 完全相同，見 4.3）。新 TX 送出的 burst，舊 RX 收不到。

表裡的數字分三種，每張表都會標明：

- **實測**：兩台 Pluto 的錄音或即時 run，有結果檔。
- **模擬**：`sim_*.py` 的輸出，有結果檔。開發時另外跑過的模擬只記在 commit message 裡，
  這裡會註明「commit 記錄」，它們沒有結果檔。
- **預測**：由模型算出來的，沒有量。

## 1. 結論

Baseline 是 `improve-rx`（commit `3ce3228`），同一天、同一個擺放位置、同一組 Pluto，在 v5 的量測前
約 1–1.5 小時量。成功標準與結果：

| # | 標準 | 目標 | 結果 |
|---|---|---|---|
| 1 | 抗雜訊：最穩的 MCS 遺失 < 1% 的最低 TX gain | 比 baseline MCS 0 的 −18.75 dB 低 2.5 dB 以上（≤ −21.25 dB） | **實測 −25 dB（MCS 8），低 6.25 dB** |
| 2 | Goodput：900-byte datagram，遺失 < 0.1%、送資料期間 0 RX overflow | TX −5 dB > 2.807 Mbit/s、TX −15 dB > 1.847 Mbit/s（baseline 的 1.2 倍） | **達成**。修正之後 −5 dB 9.295 Mbit/s（MCS 17，baseline 的 +297%）、−15 dB 5.063 Mbit/s（MCS 12，+229%）；第一輪（修正前）是 7.128／4.738 Mbit/s（+205%／+208%），當時就已達標 |
| 3 | RX overflow | 送資料期間 0 | 錄音：全部 0（v5 的 111 份 sweep 與其他 10 組錄音）；即時 run：兩輪 goodput 搜尋裡，每個最好的點都是 0 overflow（`results/v5_goodput/NOTES.md`、`results/v5_goodput_after_fix/NOTES.md`）；第一輪的 64QAM 在約 210–230 datagram/s 以上會 overflow，修正之後這些點的所有 run（含失敗的）都是 0 overflow |
| 4 | GUI 的 SNR 門檻表 | 每個開放的 v5 MCS 都有實測值 | **完成**：MCS 8–17 除了 11（見 4.1） |

### 6.25 dB 從哪裡來

MCS 8（QPSK 1/3）與 MCS 0（QPSK Turbo 1/3）的 code rate 相同，但 MCS 8 在 TX gain 低 6.25 dB 時仍然
遺失 < 1%。這 6.25 dB 分成兩半：

- **約 3 dB 是發射功率。** Pluto sink 把每個 burst 的 peak 縮放到 0.7。v5 的波形 PAPR 比較低
  （802.11a 的 STF／LTF，payload 經過 scrambler），所以縮放後的平均功率比較高。
  `tx_power_compare.py` 算出 MCS 8 的 PAPR 9.9 dB、legacy QPSK 12.9 dB，縮放後的平均功率差
  **2.95 dB（預測）**。同一個 TX gain 下，RX 量到 v5 的 in-band SNR 比 legacy 高 **3.17 dB（實測，
  中位數，範圍 2.87–3.37）**。兩者的差在 0.2–0.3 dB 內，所以這是真的功率差，不是 SNR 估計的偏差。
- **約 3 dB 是 coding 與 RX。** 用同樣的 in-band SNR 比：baseline MCS 0 的 1% 點在 legacy 讀數
  9.2 dB，換到 v5 的刻度約 9.5 dB；v5 MCS 8 是 6.7 dB。**同樣的 SNR，v5 MCS 8 少需要約 3 dB**
  （3.2 dB）。來源是 preamble（acquisition 不再是瓶頸）、兩個 LTF symbol 平均的 channel estimate，
  以及 rate matching 後的 code。

16QAM 也一樣：MCS 12（16QAM 1/2，2.0 bit/symbol）在 −18.75 dB 遺失 < 1%，比 baseline MCS 4
（16QAM 1/3，1.33 bit/symbol）的 −12.5 dB 低 6.25 dB。用同樣的 SNR 比，MCS 12 少需要 1.0 dB，
MCS 13（2.67 bit/symbol）多需要 0.3 dB：同樣的 SNR 約可送兩倍的 bit。

### 其他結論

- **MCS 8 的邊緣不再是 preamble。** 遺失 1–20% 的區間裡約三分之二是 payload 解不出來；
  SNR 低於約 3 dB（TX −28.75 dB 以下）preamble 才變成主因。Baseline MCS 0 則一直是 preamble
  （TX −22.5 dB 的 57 個遺失中 54 個）。
- **三個 64QAM 都可以用。** 這組 Pluto 最高約 29.7 dB SNR；MCS 15／16／17 遺失 < 1% 的 SNR 都比它
  低 6 dB 以上，所以開放在 TX 選單。
- **MCS 14 被 MCS 15 取代。** 兩者都是 3 bit/symbol，64QAM 1/2 只需 15.7 dB，16QAM 3/4 要 16.7 dB。
  兩個都留在選單。
- **舊 TX 的錄音在新 RX 上的結果與舊 RX 完全相同**（27 份錄音，每一點差 0.00%）。即時的
  legacy TX（improve-rx tree）→ 新 RX（H7，30 秒）：MCS 0、−15 dB、200/s 遺失 0.050%／0.000%，MCS 4、−5 dB、300/s 遺失 0.078%／0.011%，0 overflow，decoder 計數全部走 legacy 路徑（`results/v5_goodput/NOTES.md`）。與 baseline 的 RX 在同樣速率附近的遺失相同（MCS 0 171/s 0.019%、213.8/s 0.078%；MCS 4 297/s 0%、324.9/s 0.010%）。

## 2. 做法

| 項目 | 做了什麼 | 依據的標準 | 有沒有幫助 | 決定 |
|---|---|---|---|---|
| a | Preamble 改成 L-STF + L-LTF；兩種 preamble 共用一次 acquisition 掃描 | IEEE 802.11a | 有：MCS 8 的邊緣不再是 preamble | 保留 |
| b | Header 改成 SIG：K=7 convolutional code、BPSK、CRC-16，不用 ML list | IEEE 802.11a SIGNAL | 有：SIG 失敗 < 3% 的遺失 | 保留 |
| c | Scrambler x⁷+x⁴+1 | IEEE 802.11 | 必要：沒有它，全零 payload 100% 遺失 | 保留，關閉只供實驗 |
| d | Turbo code 的 rate matching、MCS 8–17、64QAM | 3GPP TS 36.212 §5.1.4.1 | 有：全部 MCS 都量到，64QAM 可用 | 保留 |
| e | A-MPDU aggregation | IEEE 802.11n | 有，而且是最大的一項：−5 dB MCS 14 開 7.128 Mbit/s、關 1.971（3.6 倍）；−15 dB MCS 12 開 4.738、關 2.268（2.1 倍） | 保留，預設開 |
| g | RX acquisition 的成本（DC／CW、短 burst） | （profile 與 bench） | 有：DC 0.003 的空 stream 556% → 24% 個 core；500 個短 burst／秒 RTF 0.85 → 1.21（目標 1.5 沒有全部達到） | 保留 |
| h | TX 最小 burst 間隔 4 ms | （H0 的 Pluto 推送模型） | 有：300 B MCS 14 −5 dB 0.534 → 6.164 Mbit/s | 保留，預設 4 ms |
| f | Guard 256／256 → 16／64 | （Pluto 實測） | 沒有可量到的差別 | 保留 16／64 |

### a. Preamble 與 acquisition

**做了什麼。** v5 的 burst 從 802.11a 的 L-STF（160 samples）與 L-LTF（32 samples GI + 兩個 64-sample
symbol）開始。STF 每 16 個 sample 重複一次，所以 lag-32 的 metric 可以累加 128 個 product，是 legacy
（32 個）的 4 倍。兩種 preamble 都是 lag-32 週期性的，所以 legacy 與 v5 共用同一組 cumulative sum，
只是 window 寬度不同（32 與 128）。v5 這一支依序做：

1. M128 ≥ 0.10（只用來限制工作量，見 4.7）。
2. 在 ±160 個 offset 內取 local peak（non-maximum suppression）。
3. 用 lag 16 估 coarse CFO（±2 subcarriers），再對 STF 做 coherent 檢查，`c_stf` ≥ 0.20。
4. 在 LTF 上找 fine timing，兩個 symbol 的相關 `c_pair` ≥ 0.25，而且兩個 symbol 各自的相關都要
   ≥ 0.5 × `c_pair`。Legacy 的 training symbol 只像 L1、不像 L2，所以這一步擋掉 legacy burst。
5. 由 L1 → L2 估 fine CFO。

兩種 candidate 太近時，留下 `confidence ÷ 自己的門檻` 比較高的那個。

LTF 也取代了 legacy 的 chirp preamble 做 channel estimation 與 SNR：兩個 symbol 平均之後的 LS
estimate 雜訊少一半；兩個 symbol 的差是雜訊（52 個自由度，legacy 是 26）。

**有沒有幫助（實測）。**

- MCS 8 在 TX −25 dB 只遺失 1 個 burst（preamble）；baseline MCS 0 在 −22.5 dB 遺失 57 個，其中 54 個
  是 preamble（4.2）。
- 兩個 LTF symbol 平均、channel estimate 平滑，各自值 1–1.5 dB（4.4）。比模擬的約 0.5 dB 大，因為
  Pluto 在邊緣的 in-band SNR 低，channel estimate 的雜訊影響比較大。
- 空中雜訊（TX 關閉 90 秒）：`c_stf` 最高 0.084、`c_pair` 最高 0.067，沒有任何一次 false acquisition（4.7）。
- 正式的 S1 模擬（`sim_s1_acq.py`）**沒有執行**：CPU 留給硬體量測。同一個問題由兩處硬體與離線結果回答：4.2 的 preamble 遺失分項，以及 rx-rate 工作時跑的 `sim_acq.py`（1000 次／點，`results/rx_rate/sim_acq_after.txt`）：AWGN −2 dB P_legacy 0.328、P_v5 0.930；0 dB 0.810、0.999；5-tap −2 dB 0.321、0.784。（`sim_acq.py` 不是 S1 的腳本，兩者沒有比對過。）開發時的模擬（commit `b04caa9` 記錄，200 次／點）
  在 AWGN 約好 3 dB、5-tap multipath 約好 2 dB。

**Acquisition 門檻維持 0.20／0.25。** 雜訊最高值加 0.1 是 0.18／0.17。在 MCS 8 的邊緣錄音上，降到
0.18／0.17 讓 preamble 漏抓少一半，但 1% 點移動不到 1.25 dB（一個量測間距），因為現在主要是 payload
在掉。較低的門檻在 legacy burst 上會不會造成 cross-family 的 false acquisition 沒有查。所以不改。

### b. SIG header

**做了什麼。** Legacy header 用 3 個 OFDM symbol 把 96 bit 以 QPSK 重複三次。SIG 用同樣 3 個 symbol
（144 個 BPSK carrier）放一個真正的 code，作法照 802.11a 的 SIGNAL field：

- 48-bit body（版本、MCS index、PSDU 長度、scrambler seed、payload 格式、保留位元）+ CRC-16/CCITT-FALSE
  + 2 個 pad + 6 個 tail = 72 bit。
- K=7、generator 133/171 的 rate-1/2 convolutional code（專案原本就有的 native Viterbi）→ 144 bit。
- 144-bit block interleaver：相鄰的 coded bit 相隔 9 個 data carrier（約 0.8 MHz），而且跨三個 symbol。
- SIG 的等化只修正共同相位（不擬合 pilot 的斜率）。開發時的模擬（commit `177ea71` 記錄）中，
  擬合斜率讓 SIG 在 2 dB 時失敗 5%，只修共同相位是 0／300。

欄位見 [docs/07 7.7](../../docs/07-reference.md#77-wire-format-v5)。

**為什麼不用 ML list。** improve-rx 在 header CRC 失敗時，會從約 8 200 個合法 header 裡選最像的一個。
v5 不這樣做：

- SIG 本身就比最穩的 payload 耐雜訊。開發時的模擬（commit `40834b5` 記錄，理想 LLR，每點 2000 個
  header）：每個 carrier SNR −1 dB 失敗 3.4%、0 dB 失敗 0.7%、+1 dB 沒有失敗。
- 合法的 SIG 有 127 個 seed × 各種長度 × 各個 MCS，約 10⁷ 種，列不完。
- 實測：MCS 8 從 TX −25 到 −30 dB（遺失 0.25% 到 96%），SIG 失敗佔遺失不到 3%；在遺失 < 1% 的點上是 0。

正式的 S2 模擬（`sim_s2_header.py`，每點 1000 次、理想 acquisition，SNR = burst 平均功率 ÷ 雜訊變異，`results/sim/s2_header.json`）。FER 1% 所需的 SNR：

| header | AWGN | multipath |
|---|---:|---:|
| v5 SIG | 0.95 dB | 1.27 dB |
| v5 SIG，沒有 interleaver | 1.43 dB | 1.90 dB |
| legacy header，ML list | 3.0 dB | 3.38 dB |
| legacy header，沒有 ML list | 6.54 dB | 低於 8 dB 沒有到 1% |

SIG 比有 ML list 的 legacy header 好約 2 dB，比沒有 list 的好 5.6 dB（AWGN）；interleaver 在 multipath 值 0.63 dB（AWGN 0.48 dB）。
但要注意：模擬的 SNR 刻度是整個 burst 的平均功率，與 4.1 的 in-band LTF SNR 不同，兩者不能直接比。在模擬的刻度上，SIG 與 MCS 8 的邊緣很近，所以模擬認為 SIG 有可能限制 MCS 8。
實機的分項卻顯示 SIG 失敗很少：只佔遺失的 0–3%（邊緣的 400 個 burst 裡 0–3 個，4.2），所以沒有加 ML list；這個差異沒有進一步查（模擬用理想 acquisition 與固定 5-tap multipath，不是實機的通道）。

### c. Scrambler

**做了什麼。** 802.11 的 x⁷+x⁴+1 LFSR，作用在 rate matching 之後的 coded bits 上（LTE 的位置，
重複的 bit 會拿到不同的 scrambling）。TX 每個 burst 換一個 seed（1–127，輪流），寫在 SIG 裡；seed 0
等於不 scramble，`phy.v5.scrambler: false` 就是送 seed 0，RX 不需要設定。

**為什麼選 802.11 而不是 LTE 的 Gold sequence。** 這裡 scrambler 只有一個工作：讓 constellation 的統計、
PAPR 與 peak 縮放後的發射功率不受 payload 內容影響。

- 週期 127 是質數，不會和每個 symbol 的 coded bit 數（96／192／288）對齊。
- 每個非零的 seed 都是同一個 m-sequence 的平移，所以整個 scrambler 是一張 127 × 127 的表加上
  `np.resize`，不必逐 bit 跑迴圈。
- LTE 的 Gold sequence 要兩個 31-bit LFSR 和 1600 步的 warm-up，在 Python 裡要逐 bit 跑（或另外寫
  native code），而對 whitening 沒有多的好處。

**有沒有幫助。**

- 模擬（S5，`results/sim/`）：5 種 payload（隨機、全零、全 0xFF、重複的 4 byte、重複的文字）縮放後的
  平均功率，沒有 scrambler 時相差 6.65–10.95 dB，有 scrambler 時 0.36–0.49 dB。
- 實測（4.6）：全零 payload、沒有 scrambler 時 PAPR 17.0 dB，縮放後少約 7 dB 的平均功率，
  TX −23.75 dB 遺失 99.8%；有 scrambler 時全零與隨機資料一樣（0.20% 與 0.10%）。

**決定：保留。** `phy.v5.scrambler` 只供實驗。

### d. Rate matching、MCS 表與 64QAM

**做了什麼。** 照 TS 36.212 §5.1.4.1（RV0）在原本的 native Turbo coder 外面包一層 rate matching：
sub-block interleaver、bit collection、circular buffer。Native coder 的輸出順序本來就是 LTE 的
d⁽⁰⁾d⁽¹⁾d⁽²⁾ 交錯（含 tail），所以 C++ 不用改；每個 code block 只是一個預先算好的 index 表。

這讓任何 code rate 都能剛好填滿整數個 OFDM symbol，**沒有 pad bit**：
`N_sym = ⌈8L ÷ (R × 48 × Qm)⌉`，L 是含 4-byte FCS 的 PSDU 長度。

新的 MCS 8–17：

| Index | 格式 | PHY rate（5 MS/s） | TX 選單 |
|---:|---|---:|---|
| 8 | QPSK 1/3 | 2.0 Mbit/s | 開放（預設） |
| 9 | QPSK 1/2 | 3.0 Mbit/s | 開放 |
| 10 | QPSK 2/3 | 4.0 Mbit/s | 開放 |
| 11 | 16QAM 1/3 | 4.0 Mbit/s | 隱藏 |
| 12 | 16QAM 1/2 | 6.0 Mbit/s | 開放 |
| 13 | 16QAM 2/3 | 8.0 Mbit/s | 開放 |
| 14 | 16QAM 3/4 | 9.0 Mbit/s | 開放 |
| 15 | 64QAM 1/2 | 9.0 Mbit/s | 開放 |
| 16 | 64QAM 2/3 | 12.0 Mbit/s | 開放 |
| 17 | 64QAM 3/4 | 13.5 Mbit/s | 開放 |

64QAM 是新加的 Gray constellation，soft demapper 對 I、Q 各自做 max-log（8 個 level），結果與 64 點的
2-D max-log 相同。

**有沒有幫助（實測，4.1）。** 每個開放的 MCS 都量到遺失 < 1% 的點，並由此訂出 GUI 門檻。同一種
modulation 內，code rate 越高需要的 SNR 越高，順序正確。

- **64QAM 開放。** 規則是「遺失 < 1% 的 SNR 比這組設備最高的 SNR 低 3 dB 以上」。最高約 29.7 dB；
  MCS 15／16／17 是 15.7／20.2／20.8 dB，都低 6 dB 以上。在 RX 修正（見下方「審查之後修的 RX 問題」）
  之前，MCS 17 要約 25 dB。
- **MCS 11 隱藏。** 它和 MCS 10 一樣是 1.33 bit/symbol，這次沒有量。
- **MCS 14 被 MCS 15 取代。** 同樣 3 bit/symbol，MCS 15 少需要 1 dB。兩個都留著。
- 正式的 S4 模擬（`sim_s4_e2e.py`）**沒有執行**：CPU 留給硬體量測。同一個問題由 4.1 的實測回答（MCS 8–17 每個都有遺失 < 1% 的 SNR，順序正確）。開發時的模擬（commit `882db4c`
  記錄，AWGN、理想 CSI、900 B、每點 200 個）BLER ≤ 1.5% 的 Es/N0：MCS 8 −0.5、9 2.0、10 4.0、11 4.0、
  12 7.0、13 9.5、14 11.0、15 11.5、16 15.0、17 16.0 dB；MCS 8 只比不做 rate matching 的 code 差約 0.1 dB。

### e. A-MPDU aggregation

**做了什麼。** 一個 v5 burst 不論帶多少資料，都要付 preamble、SIG、FCS 與 guard。所以 TX 把排隊中的
datagram 放進同一個 burst：

- PSDU = 多個 subframe + 4-byte FCS（CRC-32）。每個 subframe = 4-byte delimiter（14-bit 長度、CRC-8、
  signature 0x4E）+ 原本的 serialized frame（自己的 sequence number 與 CRC-32），對齊 4 byte。
  這是 802.11n A-MPDU 的作法。
- RX 照 delimiter 一個一個拆。Delimiter 壞了就每 4 byte 找下一個合法的 delimiter。**FCS 失敗時，
  CRC-32 正確的 subframe 照樣交付**，一個壞掉的 subframe 只損失它自己。
- TX 的規則是 opportunistic：radio 可以接下一個 burst 時，把排隊中的 datagram（FIFO）放進去，
  上限是 16 個 subframe、8192 byte、8 ms airtime。`max_wait_ms` 預設 0，所以負載低時不會多等。
  Radio 忙的時候 queue 變長，下一個 burst 就自動帶比較多。

**為什麼 airtime 上限是 8 ms。** 長 burst 有三個代價：

1. **Sample clock 漂移。** 兩個 ±25 ppm 的 crystal 最多差 50 ppm。RX 的 timing 修正最多 4 個 sample
   （`max_abs_timing_shift_samples`），所以 burst 最長 4 ÷ 50 ppm = 80 000 samples
   （`V5_MAX_TRACKED_BURST_SAMPLES`）：5 MS/s 是 16 ms、10 MS/s 是 8 ms、20 MS/s 是 4 ms。
   預設 8 ms 讓 10 MS/s 以下都在範圍內。設定檢查會拒絕超過的值，planner 不論設定多少都在
   80 000 samples 停止加 subframe。
2. Peak 縮放：burst 越長，最大的 peak 越大，縮放後平均功率越低。
3. 一個 burst 由一個 decode worker 解，burst 太大會讓交付變得一陣一陣的。

單一 datagram 超過上限時仍然單獨送出，否則 queue 會卡住。

**Pluto 能送多快（實測，H0）。** `PlutoSampleSink.send` 推 N 個 sample 要
`max(0.413 ms + 0.194 µs × N, N ÷ 5 MS/s)`（R² = 0.9994）。每次推送有約 0.4 ms 的固定成本，所以
一個 burst 一個 datagram 時，900-byte QPSK burst 最多約每秒 233 個；超過約 20 000 samples 之後才由
airtime 決定。Aggregation 的價值就在這裡。

**有沒有幫助（實測，4.8）。**

- **開與關。** 900 B、同一個 MCS 與 TX gain：−5 dB MCS 14 開 7.128、關 1.971 Mbit/s（3.6 倍）；−15 dB MCS 12 開 4.738、關 2.268（2.1 倍）。
  關掉時限制是 RX 每個 burst 的成本（約每秒 280–300 個 burst 就 overflow），不是空中容量。關掉的 −5 dB 比 baseline 的 2.339 Mbit/s 還低，−15 dB 比 baseline 高 47%。
  修正之後沒有重跑 aggregation 關。
- **900 B。** 修正之後 −5 dB 最好 9.295 Mbit/s（MCS 17），−15 dB 最好 5.063 Mbit/s（MCS 12）。
- **300 B。** 第一輪 −5 dB MCS 14 只有 0.534 Mbit/s（222/s，每個 burst 1.0 個 subframe），比 baseline 的 1.170 差；
  原因與修法見下方「第一輪 goodput 之後的兩個修正」，修正之後是 6.164 Mbit/s（2568/s，每個 burst 16.0 個 subframe）。
- **延遲。** 收發同機，包含 TX 排隊。第一輪最好的點 p50／p99：MCS 14 −5 dB 65／151 ms、MCS 12 −15 dB 55／121 ms；
  低 rate 的 MCS（8、9）的最好速率貼著 TX 容量，排隊使 p50 達 443–468 ms。修正之後 900 B 的點 p50／p99 是 67／90 到 192／250 ms
  （MCS 14 −5 dB 192／250、MCS 12 −15 dB 161／281），比修正前高，因為 4 ms 的最小間隔與在容量邊緣的排隊（`results/v5_goodput_after_fix/NOTES.md`）。
  Baseline 的延遲沒有重量。

### f. Guard

**做了什麼。** v5 burst 前後的 zero samples 從 256／256 改成 16／64（`phy.v5.guard_pre_samples`、
`guard_post_samples`）。理由是 STF 吸收開頭的 transient，後面的 zeros 讓 TX filter 輸出完。
Legacy burst 不會再送，256／256 只留在舊的 tree。

**有沒有幫助（實測）。**

- H2a（TX −10 dB，約 17 dB SNR，每組 1000 個 burst）：(0,0)、(0,32)、(16,64)、(32,128) 與 (256,256)
  看起來一樣。沒有 guard 相關的開頭 transient，結尾 1–2 個 sample 的 channel 尾巴之後就回到底噪，
  burst 結束後沒有保持住的 DC，最後一個 symbol 的 EVM 與對照組差 −0.17 到 +0.12 dB。
- 低 SNR（MCS 8、TX −26.25 dB、每組 2000 個 burst，4.5）：(16,64) 3.60%、(0,32) 3.55%、(256,256) 4.20%，
  (16,64) 再跑一次 5.40%。**兩次相同設定的 run 相差 1.8%（z = 2.7），比任何 guard 的差別都大。**
  沒有一組比 (256,256) 差。

**決定：16／64。** (0,32) 一樣可以。

### 審查之後修的 RX 問題

程式審查找到的問題，量測錄音是在修之前錄的，**4.1–4.7 的數字全部用修完的 RX（`77caa7f`）重解**。

| 問題 | 修法 | 量到（commit 記錄的重現測試） | Commit |
|---|---|---|---|
| DC offset 或 CW tone 讓 M128 整段都高於門檻，v5 的 acquisition 完全抓不到 burst | M128 減掉穩定的部分（窗兩側 448 個 product 的 smallest-of 平均）；local peak 改用 ±160 的 NMS；CFO 估計前去掉 DC 與 tone | DC 0.45：0 → 8／8 解出；tone：0 → 8／8 | `801fdbb` |
| 長 burst 的 sample clock 修正 alias：pilot 相隔 14 個 carrier，斜率超過 2.3 個 sample 就繞回去 | 每個 symbol 的斜率沿時間 unwrap（用目前為止的直線預測） | 2480 B、MCS 8、±48 ppm：0／4 → 4／4 | `d99796c` |
| 線性內插做 sample clock 修正，半個 sample 時外側 carrier 衰減 8 dB | 每個 symbol 整個平移最近的整數 sample，剩下的小數由 pilot 斜率去掉 | MCS 14、8188 B、±40 ppm：EVM 從 −27 … −17.5 dB 變成 −28.0 … −27.4 dB | `f7a49c7` |
| FFT window 從 CP 結束處開始，timing 晚一個 sample 就混進下一個 symbol | 所有 v5 FFT window 提早 3 個 sample（`phy.rx.fft_backoff_samples`） | MCS 14、timing 晚 1–3 sample：EVM −12.7／−9.6／−7.8 dB → −33 dB | `709d775` |
| 另一個 candidate 落在 v5 burst 的 SIG 內，burst 被截斷丟掉 | 落在 v5 固定部分（560 samples）內的 candidate 與它衝突，只留一個；stream 不會在 SIG 結束前切斷 v5 segment | 在 +330／+400／+550 插入假的 legacy candidate：0／1 → 1／1 | `e4eb5c3` |
| TX 的 aggregation 上限可以超過 RX 接受的 PSDU 大小，airtime 上限沒有考慮 sample clock | 設定檢查：`mac.aggregation.max_psdu_bytes` ≤ `phy.rx.max_psdu_bytes`；airtime 上限換算成 sample 數不能超過 80 000 | — | `feab715` |

另外，模擬器的 sample clock offset 改用 band-limited resampler（`c3dc197`），原本的線性內插讓長 burst
的模擬偏悲觀。

兩個已知的限制沒有修，只寫進程式的說明：

- **每個 burst 只有一個 noise variance。** Payload 的 LLR 每個 carrier 只按 channel gain 縮放。
  雜訊隨 carrier 變化時（spur、DC 漏電、頻帶邊緣的底噪），那些 carrier 的 LLR 太有自信。
  所以 `phy.rx.llr_noise_source` 沒有效果（4.4）：三個選項只改變一個共同的倍率，而 max-log Turbo
  decoder 對共同倍率不敏感。要有效，variance 必須是每個 carrier 各自的。
- **A-MPDU 的假 delimiter。** 重新同步時，隨機的 4 byte 有約 2⁻¹⁶ 的機率通過 signature 與 CRC-8。
  假的 subframe 會在 CRC-32 失敗被丟掉，但可能讓後面一個真的 delimiter 被跳過。只有 FCS 失敗的
  PSDU 會這樣掃描。

### 第一輪 goodput 之後的兩個修正

第一輪即時 goodput（`results/v5_goodput/`，tx-v5 @ `5a0cf7a`）的 900 B 結果已經達標，但有兩個弱點：

- **300 B 比 baseline 差。** −5 dB MCS 14 只有 0.534 Mbit/s（baseline 1.170）。TX 每個 datagram 都是自己的短 burst
  （`max_wait_ms` = 0 不會建立 backlog，要約 1500/s 的負載才會），burst 率停在每秒 222–300 個，RX 每個 burst 的成本使它 overflow。
  `--aggregation-max-wait-ms 4` 可以救（5.923 Mbit/s，每個 burst 14.4 個 subframe），但預設不是這樣。
- **64QAM 在約 210–230 datagram/s 就 RX overflow。** MCS 16 −5 dB 1.641、MCS 17 1.512 Mbit/s，每個 burst 只有 1.0 個 subframe，
  aggregation 幫不上（RX 先倒；搜尋在 282/s 以上看到 overflow）。

兩個修正：

1. **RX 的 acquisition 成本**（commit `7006eb6`，bench `rx_throughput_bench.py`，結果 `results/rx_rate/summary.txt`）。
   v5 的 RX 在 Pluto 上約每秒 280–300 個 burst 就 overflow（improve-rx 是 487/s）。DC offset 或 CW tone 讓 acquisition 的 metric 在每個 offset 都超過門檻，
   每個 offset 都進入 coherent 階段；另外有重複的掃描、每個大 NumPy 暫存都還給 kernel、pool 的 GIL 交接。修法：兩個 gate 去掉穩定的成分
   （乾淨的 buffer 判斷不變）、CFO phasor 分兩段建、drain 只在有新 sample 時才做、解碼 worker 保留釋放的記憶體、送進 pool 以最多 4 個一批。
   量到（RTF = stream 秒數 ÷ 實際秒數；owner = 接收執行緒佔一個 core 的比例）：

   | stream | 修正前 | 修正後 |
   |---|---|---|
   | 空 stream，DC 0.003 | owner 556% | 24% |
   | 空 stream，DC 0.01 | 884% | 38% |
   | 空 stream，tone 0.003 | 552% | 24% |
   | 乾淨雜訊，沒有 burst | 20% | 16% |
   | v5 MCS 14 300 B，500 個 burst/s | RTF 0.85（owner 86%） | RTF 1.21（64%） |
   | v5 MCS 16 900 B，500/s | RTF 1.02（76%） | RTF 1.23（62%） |
   | v5 MCS 14 300 B，300/s | owner 59%（paced 62%） | 46%（paced 44%） |
   | Pluto 式雜訊（DC 0.003、guard +10 dB），300/s | owner 90% | 61% |

   **目標沒有完全達到。** 500 個短 burst/s 的 RTF 目標是 1.5，實測 1.21–1.23；800/s 的 fast mode RTF 仍只有 0.80–0.82，owner 在 300/s 約 45%，還沒有「遠低於 50%」。
   `sim_acq.py`（1000 次／點）在修正前後輸出完全一樣，所以 acquisition 的機率沒有變。
2. **TX 最小 burst 間隔**（commit `8c1e3a6`，`mac.aggregation.min_burst_interval_ms`，CLI `--min-burst-interval-ms`，預設 4 ms，範圍 0–50，0 為關）。
   TX 把一個 burst 留到前一個 burst 交給 radio 之後至少 4 ms 才送，這段時間繼續收 datagram，仍在 subframe、byte、airtime 的上限內；
   實際等待是它與 `max_wait_ms` 較大的那個。理由：Pluto 推送一個 burst 要 0.413 ms + 0.194 µs/sample（H0），RX 的每個 burst 成本也有限，
   所以把 burst 率上限定在每秒 250 個。**延遲的取捨：** 閒置之後的第一個 burst 不多等，滿的 burst 不會被留；
   但負載高時一個 datagram 可能多等到 4 ms，容量邊緣的排隊延遲也變高（上面延遲一段）。

   **事後修正（commit `f3f84fc`）：間隔改成 start 到 start。** 上面最初的實作從前一個 burst「結束」（交給 radio）才開始算 4 ms，
   所以每個長 burst 後面多 4 ms 的空中閒置：MCS 8 一個 8 ms 的 burst 加 4 ms 空檔，airtime 約 0.67。這是 `node_app` 的 M2 量到的
   （`experiments/duplex_fdd/`）。現在從前一個 burst 的「開始」算：長 burst 本來就比 4 ms 長，不再被留住；
   短 burst 的每秒 250 個上限不變。修完之後的重量見 4.8「間隔改成 start 到 start 之後的重量」。

修正之後（`results/v5_goodput_after_fix/`，tx-v5 @ `8bb3674`，同樣的搜尋規則，每點一次搜尋）：

| 點 | goodput 修正前 → 後（Mbit/s） | 速率（/s） | 每個 burst 的 subframe | p50／p99 後（ms） | overflow |
|---|---|---|---|---|---:|
| 300 B MCS 14 −5 dB | 0.534 → **6.164** | 222 → 2568 | 1.0 → 16.0 | 80／138 | 0 |
| 900 B MCS 16 −5 dB | 1.641 → 5.589 | 228 → 776 | 1.0 → 7.8 | 67／90 | 0 |
| 900 B MCS 17 −5 dB | 1.512 → **9.295**（−5 dB 最好） | 210 → 1291 | 1.0 → 8.0 | 84／117 | 0 |
| 900 B MCS 14 −5 dB | 7.128 → 7.217 | 990 → 1002 | 7.4 → 8.0 | 192／250 | 0 |
| 900 B MCS 12 −15 dB | 4.738 → **5.063**（−15 dB 最好） | 658 → 703 | 4.6 → 6.0 | 161／281 | 0 |

64QAM 與 300 B 的點不再受 RX overflow 限制（所有 run，包含失敗的，都是 0 overflow）；現在的限制是空中與遺失，第一個失敗的速率是 2638、810、1329、1030、740 /s。
每個 burst 的 subframe 在 900 B 停在 7.8–8.0（看起來是 8 個 subframe 的上限，與 8192 byte／airtime 上限有關），300 B 約 16。
注意：MCS 16（5.589）低於 MCS 17（9.295）不合理（MCS 16 的第一個失敗速率 810/s 遺失 0.13%），可能是通道或雜訊，沒有重查。沒有跑：300 B MCS 12 −15 dB 與 aggregation 關的 MCS 14 −5 dB（時間預算）。

### 決策紀錄

| 決定 | 依據 |
|---|---|
| Scrambler 用 802.11 x⁷+x⁴+1，不用 LTE Gold | 只需要 whitening；查表、向量化；Gold 要逐 bit 的 LFSR 與 1600 步 warm-up（c 節） |
| SIG 不用 ML list | SIG 比 MCS 8 耐雜訊；合法 SIG 約 10⁷ 種；實測 SIG 失敗 < 3% 的遺失（b 節） |
| Guard 16／64 | H2a 在高 SNR 沒有差別；低 SNR 的 run-to-run 漂移（1.8%）大於 guard 的影響（f 節） |
| 開放 64QAM（15–17） | 1% 點都比最高 SNR 低 6 dB 以上（d 節） |
| MCS 11 隱藏 | 沒有量；與 MCS 10 同樣的 spectral efficiency |
| MCS 14 保留，但被 MCS 15 取代 | 同樣 3 bit/symbol，MCS 15 少需要 1 dB |
| Acquisition 門檻維持 0.20／0.25 | 雜訊最高 0.084／0.067；降低門檻 1% 點移動不到 1.25 dB；cross-family 沒有查（a 節） |
| `llr_noise_source` 維持 `max` | 三個選項結果相同：一個 burst 只有一個 noise variance，max-log Turbo 對共同倍率不敏感 |
| TX 最小 burst 間隔 4 ms | 300 B 在沒有它時 burst 率停在 222–300/s，RX overflow；有它 0.534 → 6.164 Mbit/s（上面一節） |
| 間隔從 start 到 start 算（`f3f84fc`，修正最初「從結束算」的 bug） | 從結束算會在每個長 burst 後多 4 ms 閒置（MCS 8：8 ms burst + 4 ms，airtime 約 0.67，`node_app` M2 發現）；修完重量：MCS 8 252.8/s 1.820 Mbit/s（interval 0 為 218.7/s 1.575），高 rate 的點不受影響；預設仍是 4 ms（4.8） |
| Airtime 上限 8 ms | 80 000 samples 是 50 ppm 下 RX 能修的漂移；8 ms 讓 10 MS/s 以下都安全（e 節） |

## 3. 怎麼量的

所有 Python 都以 `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1` 執行，一次只跑一個
重的工作。兩台 PlutoSDR Rev.B（韌體 v0.39，USB backend），各接原廠 antenna，同一張桌上，2.45 GHz、
5 MS/s、RX gain 15 dB，baseline 與 v5 之間沒有移動。

### Baseline 的 tree

現在的 `tx_app` 只送 v5，所以 baseline 在另一個 worktree 跑，**而且要讀它自己的 `configs/`**：

```bash
git worktree add --detach $BASE 3ce3228                   # improve-rx
cp src/ofdm_link/phy/_turbo_native*.so $BASE/src/ofdm_link/phy/
cd $BASE && PYTHONPATH=src python experiments/rx_improvement/ota_capture.py --out caps_baseline \
    --mcs qpsk --tx-gains -30 -27.5 -25 -22.5 -20 -17.5 -15 -12.5 -10 -7.5 -5 -2.5 0 \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
cd $BASE && PYTHONPATH=src python experiments/rx_improvement/offline_decode.py caps_baseline ota_baseline.json
```

`live_run.sh` 與 `goodput_search.py` 用第一個參數（`ROOT`／`--root`）選要跑哪一個 tree，所以同一組
腳本量 baseline 與 v5。

### 錄音與離線解碼

1. `ota_capture_v5.py`：用現在的 `MessageTransmitter` 送 400 個 900-byte burst（每秒 100 個，seed 0–399，
   與 baseline 相同），另一台錄原始 IQ（int16，**沒有放進 repo**，約 15 GB）。Aggregation 關閉，
   所以遺失是以 burst 計。可選 `--guards PRE POST`、`--scrambler on|off`、`--payload random|zeros`。
2. `offline_decode_v5.py`：把錄音以 65 536 samples 一塊餵進 app 的 `MessageReceiver`（inline，
   `--workers 1`），並記錄每一次 decode 嘗試。每個遺失的 sequence 用前後解出的 burst 內插出位置，
   由附近的嘗試判斷它掉在哪一關：preamble 沒抓到、SIG 失敗、payload 失敗。`--set key=value`
   可以改設定（RX 的 ablation 用這個）。
3. `decode_all_v5.sh` 跑全部的離線解碼；`analyze_v5.py` 產生 `results/v5/summary.md`。
4. `noise_scan_v5.py record` 錄 TX 關閉時的空中訊號，`analyze` 離線算每個 offset 的 M128 與每個 peak
   的 `c_stf`／`c_pair`。
5. `tx_power_compare.py`：在兩個 tree 各跑一次，算 peak 縮放後的平均功率與 PAPR。
6. `h0_push_model.py`（只發射）、`h2a_guards.py`：Pluto 的推送時間與 burst 邊緣。

```bash
python experiments/tx_v5/ota_capture_v5.py --out $S/caps_v5/sweep --mcs-index 8 \
    --tx-gains -30 -28.75 -27.5 -26.25 -25 --enable-rf \
    --acknowledgement 'I acknowledge that this process will transmit RF'
python experiments/tx_v5/ota_capture_v5.py --out $S/caps_v5/guards --mcs-index 8 --tx-gains -26.25 \
    --bursts 2000 --guards 16 64 --tag g16_64 --enable-rf --acknowledgement '...'
bash experiments/tx_v5/decode_all_v5.sh
python experiments/tx_v5/analyze_v5.py > experiments/tx_v5/results/v5/summary.md
python experiments/tx_v5/tx_power_compare.py v5 8       # 這個 tree；baseline tree 跑 legacy qpsk
```

### 即時 goodput

- `live_run.sh ROOT TX_GAIN MCS_INDEX RATE SECONDS OUT [SIZE]`：從 `ROOT` 啟動真的 `rx_app` 與 `tx_app`，
  用 `../ota_udp_throughput/udp_load.py` 以固定速率送 datagram 並逐 byte 比對。`cpu_sampler.py`
  記錄 decode worker 的 CPU。
- `goodput_search.py`：先送一個低速率的探測，再以 1.5 倍往上找、二分到約 5% 的解析度。
  **Goodput = 遺失 < 0.1% 而且送資料期間 0 RX overflow 的最高速率 × datagram 大小**，每次 30 秒。
  每次開始前等 1 分鐘的 load < 4；失敗而且同時有 overflow 或高 load 的 run 會重跑一次。
- `run_baseline_goodput.sh`：baseline 的 16 組（2 個 TX gain × 8 個 MCS），之後加 300-byte 兩組。
- v5：第一輪在 `results/v5_goodput/`（`NOTES.md`、`goodput_v5.json`；H7 在 `h7/`），修正之後的重跑在 `results/v5_goodput_after_fix/`（`NOTES.md`、`goodput_after_fix.json`，腳本 `run_v5_after_fix.sh`）。同樣的搜尋規則；`goodput_search.py` 另外有 `--tx-arg`、`--tx-root`、`--est-rate`、`--cap`，並記錄延遲與每個 burst 的 subframe 數。

### 模擬

`sim_common.py` 是共用的 channel（zero padding、可選的固定 5-tap multipath、10 kHz CFO、白雜訊）。
**模擬只在沒有硬體量測時跑。**

| # | 問題 | 腳本 | 狀態 |
|---|---|---|---|
| S1 | v5 與 legacy 的 acquisition | `sim_s1_acq.py`（寫好，**未執行**） | 未執行（CPU 留給硬體量測）；見 4.9 |
| S2 | SIG 與 legacy header 的 FER | `sim_s2_header.py` | 完成，見 b 節 |
| S3 | LTF 平均與 channel smoothing | `sim_s3_estimator.py`（寫好，**未執行**） | 未執行；見 4.9 |
| S4 | Rate matching、各 MCS 的曲線 | `sim_s4_e2e.py`（寫好，**未執行**） | 未執行；見 4.9 |
| S5 | Scrambler 對 PAPR 與發射功率 | `sim_s5_scrambler.py` | 完成，見 c 節 |
| S7 | LTF-pair SNR 估計的偏差與跳動 | —（沒有寫） | 未執行；見 4.9 |
| S8 | CPU：各 MCS 的 decode 時間、acquisition 的成本 | —（沒有寫） | 未執行；見 4.9 |

## 4. 數據

### 4.1 錄音：每個 MCS 遺失 < 1% 的最低 TX gain（實測）

每點 400 個 900-byte burst，TX gain 間距 2.5 dB，邊緣 1.25 dB。SNR 是該點 v5 LTF 估計的 in-band SNR 中位數。
門檻 = SNR + 1 dB，四捨五入到 0.5 dB（與 improve-rx 相同的規則）。

| MCS | 格式 | 最低 TX gain | 該點遺失 | SNR | 下一個較低的點 | GUI 門檻 |
|---:|---|---:|---:|---:|---|---:|
| baseline 0 | QPSK Turbo 1/3 | −18.75 dB | 0.25% | 9.2 dB（legacy 刻度） | −20 dB：1.00% | 9.0 dB |
| baseline 4 | 16QAM Turbo 1/3 | −12.5 dB | 0 | 13.7 dB（legacy 刻度） | −15 dB：1.00% | 15.0 dB |
| 8 | QPSK 1/3 | **−25 dB** | 0.25% | 6.7 dB | −26.25 dB：3.50% | 7.5 dB |
| 9 | QPSK 1/2 | −23.75 dB | 0.25% | 7.9 dB | −25 dB：3.75% | 9.0 dB |
| 10 | QPSK 2/3 | −21.25 dB | 0.50% | 10.2 dB | −22.5 dB：2.75% | 11.0 dB |
| 11 | 16QAM 1/3 | 沒有量 | | | | — |
| 12 | 16QAM 1/2 | **−18.75 dB** | 0.25% | 13.0 dB | −20 dB：2.25% | 14.0 dB |
| 13 | 16QAM 2/3 | −17.5 dB | 0.75% | 14.3 dB | −18.75 dB：9.25% | 15.5 dB |
| 14 | 16QAM 3/4 | −15 dB | 0.25% | 16.7 dB | −16.25 dB：4.50% | 17.5 dB |
| 15 | 64QAM 1/2 | −16.25 dB | 0.75% | 15.7 dB | −17.5 dB：5.00% | 16.5 dB |
| 16 | 64QAM 2/3 | −11.25 dB | 0.25% | 20.2 dB | −12.5 dB：1.25% | 21.0 dB |
| 17 | 64QAM 3/4 | −11.25 dB | 0.50% | 20.8 dB | −12.5 dB：2.50% | 22.0 dB |

- 1.25 dB 的間距加上 400 個 burst，每個交叉點約有 ±0.6 dB 的不確定。
- MCS 13 在 −15 dB 另外掉了 2 個（0.50%，payload，單獨出現）。
- 每個 MCS 每個點的完整表在 `results/v5/summary.md`；原始數據 `results/v5/ota_v5.json`、
  `results/baseline/ota_baseline.json`。

### 4.2 遺失掉在哪一關（實測）

| 點 | 遺失 | preamble／SIG／payload |
|---|---:|---|
| MCS 8，−25 dB | 0.25% | 1／0／0 |
| MCS 8，−26.25 dB | 3.50% | 4／0／10 |
| MCS 8，−27.5 dB | 21.0% | 30／0／54 |
| MCS 8，−28.75 dB | 70.0% | 118／3／159 |
| MCS 8，−30 dB | 96.25% | 231／9／145 |
| baseline MCS 0，−22.5 dB（新 RX） | 14.25% | 54／1／2 |
| MCS 12，−20／−21.25 dB | 2.25%／8.25% | 1／0／8、0／0／33 |
| MCS 14，−16.25 dB | 4.50% | 0／0／18 |

### 4.3 發射功率與 SNR 的刻度（實測與預測）

| | MCS 8 vs legacy MCS 0 | MCS 12 vs legacy MCS 4 |
|---|---:|---:|
| PAPR（預測，200 個 burst 的中位數） | 9.9 vs 12.9 dB | 9.5 vs 14.1 dB |
| Peak 縮放到 0.7 後的平均功率（預測） | −13.01 vs −15.96 dBFS | −12.58 vs −17.18 dBFS |
| 平均功率差（預測） | **+2.95 dB** | **+4.65 dB** |
| 同一個 TX gain 的 SNR 讀數差（實測，TX −22.5 … 0 dB 的中位數） | **+3.17 dB**（2.87–3.37） | **+4.93 dB**（4.35–5.29） |
| EVM SNR 差（實測，TX −22.5 … −10 dB） | +3.2 dB（2.6–4.2） | +4.3 dB（3.5–4.8） |

Legacy 的 chirp preamble 在 64 個 bin 上是平的，所以它的 in-band 功率與資料相同，和 v5 的 LTF 一樣。
預測與實測只差 0.2–0.3 dB，在中位數的跳動範圍內。

Legacy 錄音在新 RX 上（`legacy_on_new_rx.json`；`legacy_on_new_rx_v5acq_off.json` 是關掉 v5
acquisition 的版本）：27 份錄音（MCS 0 與 4，TX −30 … 0 dB）的遺失與 SNR 中位數，與 baseline 的 RX
**完全相同**，兩種設定都一樣。

### 4.4 RX ablation（實測，離線重解同一份錄音）

解出的 burst 數（每點 400 個）：

| 設定 | MCS 8：−28.75／−27.5／−26.25／−25 dB | MCS 12：−23.75／−22.5／−21.25／−20 dB |
|---|---|---|
| 預設 | 120／316／386／399 | 159／269／367／391 |
| `ltf_average=false`（只用 L1） | 42／204／328／384 | 58／161／301／362 |
| `channel_smoothing=false` | 52／242／359／396 | 54／140／306／372 |
| `llr_noise_source=header` | 與預設相同 | 與預設相同 |
| `llr_noise_source=ltf` | 121／316／386／399 | 159／269／368／393 |

### 4.5 Guard（實測，MCS 8，TX −26.25 dB，每組 2000 個 burst）

| (pre, post) | 順序 | 遺失 | 95% CI（Wilson） | preamble／SIG／payload |
|---|---:|---:|---|---|
| (16, 64) 預設 | 1 | 3.60% | 2.87–4.51% | 13／0／59 |
| (0, 32) | 2 | 3.55% | 2.82–4.45% | 29／0／42 |
| (256, 256) | 3 | 4.20% | 3.41–5.17% | 34／0／50 |
| (16, 64) 再一次 | 4 | 5.40% | 4.49–6.48% | 50／0／58 |

### 4.6 Scrambler（實測，MCS 8，每組 1000 個 burst）

| TX gain | Payload／scrambler | TX PAPR | 遺失 | 95% CI | SNR |
|---:|---|---:|---:|---|---:|
| −26.25 dB | 隨機／開 | 10.0 dB | 3.50% | 2.53–4.83% | 5.3 dB |
| −26.25 dB | 全零／開 | 9.8 dB | 4.00% | 2.95–5.40% | 5.4 dB |
| −26.25 dB | 全零／關 | 17.0 dB | 100% | 99.6–100% | — |
| −23.75 dB | 隨機／開 | 10.0 dB | 0.10% | 0.02–0.56% | 8.1 dB |
| −23.75 dB | 全零／開 | 9.6 dB | 0.20% | 0.05–0.73% | 8.0 dB |
| −23.75 dB | 全零／關 | 17.0 dB | 99.8% | 99.3–99.95% | 1.0 dB |

### 4.7 空中雜訊與 acquisition 門檻（實測）

TX 關閉，RX gain 15 dB，90 秒全部離線處理：

| 分數 | 雜訊最高 | 設定的門檻 |
|---|---:|---:|
| v5 M128（去掉穩定部分） | 0.89 | 0.10 |
| v5 `c_stf`（通過 M128 的 peak） | 0.084 | 0.20 |
| v5 `c_pair`（通過 M128 的 peak） | 0.067 | 0.25 |
| legacy coherent confidence | 0.201 | 0.30 |
| `acquire_all` 的 candidate | v5 0、legacy 0 | |

M128 在 Pluto 的雜訊上（有顏色、guard band 雜訊高）會到 0.9，所以它只限制工作量（每秒約 4 500 個
peak 進入 STF 檢查），區分雜訊與 preamble 的是後面兩個 coherent 檢查。

MCS 8 邊緣錄音在三組門檻下解出的 burst（括號是 preamble 漏抓，`threshold_ablation.json`）：

| TX gain | 0.20／0.25（設定） | 0.18／0.17 | 0.12／0.12 |
|---:|---:|---:|---:|
| −30 dB | 15（231） | 20（152） | 21（116） |
| −28.75 dB | 120（118） | 134（64） | 136（47） |
| −27.5 dB | 316（30） | 328（15） | 330（10） |
| −26.25 dB | 386（4） | 389（0） | 389（0） |
| −25 dB | 399（1） | 400（0） | 400（0） |

### 4.8 即時 goodput

Baseline（實測，`results/baseline/goodput_baseline.json`）：

| | TX −5 dB | TX −15 dB |
|---|---:|---:|
| 900 B，最好的 MCS | **2.339 Mbit/s**（MCS 4，325/s） | **1.539 Mbit/s**（MCS 0，214/s） |
| 900 B，MCS 0 | 1.539 Mbit/s（214/s） | 1.539 Mbit/s（214/s） |
| 300 B | 1.170 Mbit/s（MCS 4，487/s） | 0.952 Mbit/s（MCS 0，397/s） |
| RX overflow（送資料期間） | 0 | 0 |

Convolutional 與 uncoded 的 MCS（1、2、3、5、6、7）在 900 B 幾乎沒有 goodput；MCS 4 在 −15 dB
（SNR 11.3 dB）的探測就掉了 1.8%。

v5，900 B，aggregation 開，**第一輪（修正前）**（實測，`results/v5_goodput/NOTES.md`）。MCS 13／14／15 在 −15 dB 貼著 SNR 邊緣，13 與 14 只有不到 400 個 datagram，只作參考。第一輪的最好值：−5 dB MCS 14 7.128、−15 dB MCS 12 4.738 Mbit/s。MCS 16、17 在 −5 dB 的 1.0 個 subframe 是 RX overflow 造成的（見「第一輪 goodput 之後的兩個修正」）：

| MCS | TX −5 dB goodput | TX −15 dB goodput | 每個 burst 的 subframe 數（−5／−15 dB） |
|---:|---:|---:|---:|
| 8 | 1.748 Mbit/s | 1.748 Mbit/s | 2.0／2.0 |
| 9 | 2.668 Mbit/s | 2.668 Mbit/s | 3.0／3.0 |
| 10 | 3.308 Mbit/s | 3.496 Mbit/s | 3.2／4.0 |
| 12 | 5.103 Mbit/s | 4.738 Mbit/s | 6.0／4.6 |
| 13 | 6.237 Mbit/s | 0.090 Mbit/s（只有 375 個 datagram） | 6.3／1.0 |
| 14 | 7.128 Mbit/s | 0.032 Mbit/s（只有 132 個 datagram） | 7.4／1.0 |
| 15 | 6.480 Mbit/s | 0.895 Mbit/s（遺失 0.080%，在邊緣） | 8.0／1.0 |
| 16 | 1.641 Mbit/s | 未通過（20/s 探測遺失 24%） | 1.0／— |
| 17 | 1.512 Mbit/s | 未通過（20/s 探測遺失 60%） | 1.0／— |

| 項目 | 結果 |
|---|---|
| **修正之後**，900 B 最好 | −5 dB 9.295 Mbit/s（MCS 17，1291/s，8.0 subframe/burst）、−15 dB 5.063 Mbit/s（MCS 12，703/s）；對 baseline +297%／+229%（第一輪 +205%／+208%） |
| 修正之後，其他點 | 900 B MCS 16 −5 dB 5.589；MCS 14 −5 dB 7.217；300 B MCS 14 −5 dB 6.164 Mbit/s（修正前 0.534） |
| 300 B，最好的 MCS，TX −5 dB | 第一輪 MCS 14 0.534 Mbit/s（baseline 1.170，較差）；`--aggregation-max-wait-ms 4` 5.923（14.4 subframe/burst）；**修正之後 6.164 Mbit/s**（2568/s，16.0 subframe/burst） |
| 300 B，最好的 MCS，TX −15 dB | MCS 12 4.252 Mbit/s（1772/s，13.2 subframe/burst，baseline 0.952）；修正之後沒有重跑 |
| Aggregation 開與關（同一個 MCS 與 TX gain，900 B，第一輪） | −5 dB MCS 14：開 7.128、關 1.971 Mbit/s（3.6 倍）；−15 dB MCS 12：開 4.738、關 2.268（2.1 倍）。只量最好的 MCS，關的另一個 MCS 可能略好 |
| 延遲（收發同機，p50／p99，含 TX 排隊） | 第一輪最好的點：MCS 12–14 為 55／121 到 65／151 ms（MCS 12 與 15 在 −5 dB 的 p99 達 358／353 ms）；低 rate 的 MCS 8、9 貼著 TX 容量，p50 為 440–470 ms。修正之後：67／90 到 192／250 ms（容量邊緣的排隊與 4 ms 間隔使 900 B MCS 14 與 12 變高）。Baseline 沒有重量 |
| 送資料期間的 RX overflow | 兩輪每個最好的點都是 0。第一輪 64QAM 在約 210–230/s 以上會 overflow（搜尋在 282/s 以上看到）；修正之後所有 run 都是 0 |
| 即時 legacy TX（improve-rx tree）→ 新 RX（H7，30 秒×2） | MCS 0、−15 dB、200/s：遺失 0.050%、0.000%；MCS 4、−5 dB、300/s：0.078%、0.011%；0 overflow；全部走 legacy decoder（與 baseline RX 在附近速率的 0.019–0.078%、0–0.010% 相同） |

#### 間隔改成 start 到 start 之後的重量（實測）

`results/v5_goodput_mcs8_recheck/`（`NOTES.md`、`recheck.json`），tx-v5 @ `f3f84fc`，900 B、TX −15 dB、RX gain 15 dB、`goodput_search.py`
（每次 30 秒，通過 = 遺失 < 0.1% 而且送資料期間 RX overflow = 0）。所有 run 的送資料期間 RX overflow 都是 0；每次開始時 1 分鐘 load 0.75–3.72（< 4）。

| 點 | 間隔之前（第一輪，沒有間隔） | 之後（4 ms，start 到 start） | 備註 |
|---|---|---|---|
| MCS 8 | 243/s，1.748 Mbit/s | **252.8/s，1.820 Mbit/s**（259.7/s 在 0.33% 失敗） | 到上限時約 2.0 個 subframe/burst；沒有退步 |
| MCS 8，interval 0 | （243/s） | 218.7/s，1.575 Mbit/s（225.5/s 在 1.0% 失敗；246/s 在 0.11%） | 比預設 4 ms 差：間隔讓 MCS 8 多聚合、少 burst |
| MCS 10 | 486/s，3.496 Mbit/s | 283.1/s，2.038 Mbit/s（291.6/s 在 0.103% 失敗，邊緣） | 搜尋停得太早，見下面的直接重跑 |

MCS 8 的 252.8/s 與上面預測的放滿上限 254/s（1.83 Mbit/s）只差 0.5%。

MCS 10 的搜尋停在 283/s，失敗的那一點只有 0.103%（門檻 0.1%）。所以另外直接跑第一輪的速率 486/s（30 秒，900 B，TX −15 dB，`live_run.sh`，`TX_EXTRA_ARGS="--min-burst-interval-ms N"`）：

| run（順序） | 間隔 | 遺失 | Goodput | subframe／PSDU | PSDU FCS 失敗 |
|---|---|---|---|---|---|
| `direct_mcs10_486/int4`（第 1） | 4 ms | 3.573% | 3.374 Mbit/s | 3.58 | 193 |
| `direct_mcs10_486/int0`（第 2） | 0 | 0.000% | 3.499 Mbit/s | 3.72 | 0 |
| `direct_mcs10_486/rep_int0`（第 3） | 0 | 0.000% | 3.499 Mbit/s | 3.83 | 0 |
| `direct_mcs10_486/rep_int4`（第 4） | 4 ms | 0.021% | 3.499 Mbit/s | 3.84 | 1 |

四次裡 3 次通過（3.50 Mbit/s）；第 1 次的 3.6% 是離群值，反過來順序重跑沒有重現。遺失全是空中的（FCS 失敗），0 RX overflow。
每個 PSDU 3.6 個 subframe 時 burst 約 7.7 ms，比間隔長，間隔根本沒有留住它們。這一段（Wi-Fi）的 run-to-run 變化比任何間隔的影響都大。
結論：MCS 10、−15 dB 仍通過 486/s（3.50 Mbit/s），283/s 是搜尋的雜訊停點，不是退步。沒有量：MCS 10 在 interval 0 的搜尋（時間預算）。

高 rate 的點，interval 0 對 4 ms，TX −5 dB（`direct_int0_check/`）：300 B MCS 14 在 2568/s 都是 0% 遺失、6.163 Mbit/s，
每個 PSDU 15.0 對 15.4 個 subframe；900 B MCS 17 在 1291/s 遺失 0.031% 對 0.010%，兩者都是 9.29 Mbit/s。
RX 修正之後這些點不再需要間隔，但間隔沒有害處，而且 MCS 8 有它走得更遠（252.8/s 對 218.7/s）。**預設維持 4 ms。**

預測的上限（**沒有量**）：只算 Pluto 的推送時間（H0 的模型），不算 Python 的 TX worker 與 RX 的 CPU。
Baseline MCS 4 用同一個模型算出每秒 408 個，實測在約 335 個飽和，所以實際值會比下表低。
Aggregation 依預設上限（16 個、8192 B、8 ms）把 900-byte datagram 放滿一個 burst：

| MCS | 一個 burst 一個：samples／datagram 每秒／Mbit/s | 放滿：datagram 數／airtime | 放滿：datagram 每秒／Mbit/s |
|---:|---|---|---|
| 8 | 19 600／237／1.71 | 2／7.70 ms | 254／1.83 |
| 9 | 13 280／335／2.41 | 3／7.70 ms | 381／2.74 |
| 10 | 10 160／420／3.02 | 4／7.70 ms | 508／3.66 |
| 12 | 6 960／567／4.09 | 6／7.70 ms | 762／5.49 |
| 13 | 5 440／681／4.91 | 8／7.70 ms | 1016／7.31 |
| 14、15 | 4 880／736／5.30 | 8／6.85 ms | 1134／8.17 |
| 16 | 3 840／864／6.22 | 8／5.17 ms | 1475／10.62 |
| 17 | 3 520／913／6.57 | 8／4.61 ms | 1639／11.80 |

MCS 13 以上被 8192-byte 的上限限制在 8 個。這些數字由 `MessageTransmitter.plan_burst` 與
`v5_burst_samples_with_guards` 算出。

### 4.9 模擬

| # | 結果 |
|---|---|
| S1 | **未執行**：CPU 留給硬體量測。同一個問題的硬體與離線結果：4.2 的 preamble 遺失分項（MCS 8 −25 dB 只遺失 1 個 burst，baseline MCS 0 −22.5 dB 的 57 個遺失中 54 個是 preamble）；另有 `sim_acq.py`（1000 次／點，`results/rx_rate/sim_acq_after.txt`）：AWGN −2 dB P_legacy 0.328／P_v5 0.930、0 dB 0.810／0.999；5-tap −2 dB 0.321／0.784；cross-family 計數全是 0 |
| S2 | 完成，`results/sim/s2_header.json`：FER 1% 的 SNR，v5 SIG 0.95 dB（AWGN）／1.27 dB（multipath），legacy 加 ML list 3.0／3.38 dB，legacy 沒有 list 6.54 dB／低於 8 dB 未到；interleaver 在 multipath 值 0.63 dB。模擬認為 SIG 可能限制 MCS 8，實機的 SIG 失敗只佔遺失 0–3%，所以沒有加 ML list（b 節） |
| S3 | **未執行**（`sim_s3_estimator.py` 寫好沒跑）。硬體結果：RX ablation，兩個 LTF symbol 平均與 channel smoothing 各自值 1–1.5 dB（4.4） |
| S4 | **未執行**（`sim_s4_e2e.py` 寫好沒跑）。硬體結果：4.1 每個 MCS 的實測 SNR 門檻 |
| S5 | c 節；`results/sim/s5_scrambler.json` |
| S7 | **未執行**。實測的 SNR 刻度分析：`results/v5/tx_power_compare.jsonl` 與 4.3（v5 的 LTF 估計比 legacy 高 3.17 dB，真的功率差） |
| S8 | **未執行**。CPU 的實測：`results/rx_rate/summary.txt`（各 MCS 與 acquisition 的 RTF、owner%，見「第一輪 goodput 之後的兩個修正」） |

## 5. 檔案

| 檔案 | 內容 |
|---|---|
| `results/baseline/NOTES.md` | Baseline 的過程、指令與異常 |
| `results/baseline/ota_baseline.json`、`captures/*.json` | Baseline 錄音（27 份）的離線解碼結果與每份錄音的 metadata |
| `results/baseline/goodput_baseline.json`、`goodput/` | Baseline 即時 goodput：每次 run 的 `load.json`、`meta.json`、`rx_stats.jsonl` |
| `results/hw_char/README.md`、`h0_push_model.json`、`h2a_guards.json` | Pluto 的推送時間與 burst 邊緣 |
| `results/v5/NOTES.md`、`summary.md` | v5 錄音的過程、指令與完整的表 |
| `results/v5/ota_v5.json` | v5 sweep（MCS 8–17，111 份錄音） |
| `results/v5/legacy_on_new_rx*.json` | Baseline 錄音在新 RX 上，v5 acquisition 開與關 |
| `results/v5/guards_low_snr.json`、`scrambler_ota.json` | Guard 與 scrambler |
| `results/v5/rx_ablation.json`、`threshold_ablation.json` | RX 設定與 acquisition 門檻的 ablation |
| `results/v5/noise_scan.json` | 空中雜訊掃描 |
| `results/v5/tx_power_compare.jsonl` | v5 的 PAPR 與縮放後的功率（legacy 的數字在 `NOTES.md`） |
| `results/v5/captures/` | 每份錄音的 metadata（load、overflow、late send） |
| `results/v5_goodput/` | v5 即時 goodput，第一輪（`NOTES.md`、`goodput_v5.json`、`h7/`） |
| `results/v5_goodput_after_fix/` | 兩個修正之後的即時 goodput（`NOTES.md`、`goodput_after_fix.json`） |
| `results/v5_goodput_mcs8_recheck/` | 間隔改成 start 到 start 之後的低 MCS 重量（`NOTES.md`、`recheck.json`） |
| `results/rx_rate/` | RX 的 CPU 與 acquisition bench（`summary.txt`） |
| `results/sim/README.md`、`s2_header.json`、`s5_scrambler.json` | 模擬（S2、S5） |

原始 IQ（baseline 27 份、v5 約 15 GB）沒有放進 repo。

## 6. 證據邊界

- 只有 Pluto 對 Pluto、一個擺放位置、一天。B210、N210、USRP-2901 沒有量 v5。
- 錄音每點 400 個 burst，遺失 0.25% 就是 1 個；邊緣的點另外有約 2% 的 run-to-run 漂移（4.5）。
- Baseline 與 v5 的錄音相隔約 1–1.5 小時，不是同一份錄音（wire format 不同，無法用同一份比）。
- 4.1 的 v5 SNR 是 LTF 的估計，baseline 是 legacy preamble 的估計，同一個 TX gain 差約 3 dB；
  這個差是真的功率差（4.3），但兩種 SNR 不要直接比，用 TX gain 或換算後的刻度比。
- MCS 11 沒有量。64QAM 只在這組設備最高約 29.7 dB 的 SNR 下量過。
- 開發時的模擬與 CPU 數字（commit `b04caa9`、`40834b5`、`177ea71`、`882db4c`、`801fdbb`）只記在 commit
  message 裡，腳本與輸出沒有進 repo。S2、S5 有結果檔；S1、S3、S4、S7、S8 沒有執行，見 4.9。
- RX 的 payload LLR 每個 burst 只用一個 noise variance；雜訊隨 carrier 變化的環境沒有測。
- 即時 goodput 每點一次搜尋（遺失 0.1% 在邊緣只有 3–30 個 datagram，通過與否有雜訊）；第一輪與修正之後是不同的 commit，修正之後只重跑 5 個點（沒有 aggregation 關、沒有 300 B MCS 12）；MCS 16 < MCS 17 沒有重查。
- 間隔改成 start 到 start 之後只重量低 MCS 的三個點（MCS 8、MCS 10、MCS 8 interval 0；4.8），高 rate 的點只比對 interval 0 與 4 ms 各一次。
  MCS 10 的搜尋在 283/s 以 0.103% 的邊緣遺失停下，直接跑 486/s 的 4 次中 3 次通過、1 次 3.6% 離群值沒有重現：Wi-Fi 造成的 run-to-run 變化比間隔的影響還大，
  所以 4 ms 與 0 的差別在這個雜訊內只能說「沒有退步」，MCS 10 在 interval 0 的搜尋沒有量。
- RX 的 CPU 目標（500 個短 burst/s RTF 1.5）沒有達到，只有 1.21–1.23；S1、S3、S4、S7、S8 的模擬沒有跑（4.9）。

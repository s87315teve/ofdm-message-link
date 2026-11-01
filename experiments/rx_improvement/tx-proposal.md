# 提案：接下來可以改 TX 的地方（未實作）

> **狀態（2026-11-01）：這些提案已在 `tx-v5` 分支實作（wire v5），見 [Wire v5 報告](../tx_v5/README.md)。**
> 第 1 項（更長的 preamble）→ 802.11a 的 L-STF + L-LTF；第 2 項（header 分散到不同頻率）→ SIG 改用
> K=7 convolutional code 加 144-bit interleaver；第 3 項改法 A（第二個 training symbol）→ 兩個 L-LTF symbol
> 平均的 channel estimate。下面保留提案當時的原文與數字。

這份只是提案。`improve-rx` 分支只改了 RX，wire format 和 v1.0.0 完全相同，舊的 TX 照樣能用。
下面每一項都要改 wire format、升 protocol 版本，新舊兩端就不能互通，所以先寫下來，等你決定。

## 現在 RX 改完之後，burst 是在哪裡掉的

實機錄音（[`results/ota_new.json`](results/ota_new.json)，每點 400 個 900 B burst，最終版 RX）：

| 條件 | SNR | 遺失 | preamble 沒抓到 | header 失敗 | payload 失敗 |
|---|---:|---:|---:|---:|---:|
| MCS 0，TX −22.5 dB | 5.6 dB | 66 | **61** | 0 | 5 |
| MCS 0，TX −25 dB | 3.5 dB | 327 | **224** | 3 | 100 |
| MCS 4，TX −17.5 dB | 8.9 dB | 78 | 1 | 1 | **77** |
| MCS 4，TX −20 dB | 6.7 dB | 302 | 27 | 1 | **275** |

「preamble 沒抓到」是 400 減去有 preamble SNR 讀數的 burst 數。「header 失敗」也包含不是 burst 的多餘候選，
所以三欄加起來可能比遺失多 1。

Header 已經不是問題：maximum-likelihood header 把它從最先失效的環節變成最不會失效的環節。
MCS 0 先失效的是 **acquisition**，MCS 4 是 **payload**。所以 TX 端的改動，優先順序跟改 RX 之前不一樣了。

## 1. 更長的 preamble：讓 0–2 dB 也抓得到 burst（建議優先）

- **現況**：Schmidl–Cox 用兩個 32-sample 的半段做 correlation。門檻已經降到 0.25／0.30，
  再降就會碰到雜訊（空中雜訊的 correlation 最高 0.20）。MCS 0 在 TX −22.5 dB 遺失的 burst
  九成是 preamble 沒找到，後面再強也沒用。
- **改法**：半段加長成 64 sample（preamble 從 80 變成 144 sample），或者送兩次 preamble。
  Correlation 的雜訊會小約 3 dB，`detection_threshold` 也可以跟著降。
- **代價**：MCS 0 一個 900 B burst 約 19 400 sample，多 64 sample 是 0.3% 的 airtime。
- **預期**：acquisition 的門檻往下移約 2–3 dB。MCS 0 的 payload 在 TX −25 dB 時也開始失敗，
  所以實際好處大約是 1–2 dB；如果之後加更低碼率的 MCS，這項是必要的。

## 2. Header 重複分散到不同頻率：multipath 下的保險

- **現況**：每個 header bit 重複 3 次，但放在**相鄰的位置**，同一個 QPSK symbol 就帶了兩份。
  遇到 frequency-selective fading 時三份一起壞，沒有頻率分集。
- **改法**：第 k 份放在相隔 16 個 subcarrier、或不同 OFDM symbol 的位置。
- **代價**：零 airtime，只是重新排列。
- **預期**：實機與模擬中 header 都已經不是瓶頸，所以短期收益小；
  在 fading 更深的環境（例如人走動造成的深衰落）才會看得出來。可以和第 1 項一起升版本，
  不必單獨做。

## 3. 第二個 training symbol 或 boosted pilot：channel estimate 更準

- **現況**：channel estimate 只來自一個 training symbol。RX 已經用 delay-limited fit
  把估計誤差壓低約 4 dB，但等化後 noise 仍放大約 1.4 dB（`1 + 20/52`）。
- **改法 A**：多一個 training symbol（+80 sample，約 0.4% airtime），估計誤差再少 3 dB，
  等化損失從約 1.4 dB 降到約 0.8 dB。
- **改法 B**（SISO 的做法）：pilot 加大功率、加密，每個 symbol 重估 channel。
  對時變 channel（移動中）才有意義，現在桌上的測試用不到，代價是 data 功率變少。
- **預期**：A 大約 0.5 dB，主要幫 MCS 4（它的瓶頸是 payload）；B 在靜態 channel 下可能反而變差。

## 不建議照搬的 SISO 設計

- **連續發射**：RX 好同步，但不能 TDD、不能多台共用頻道，閒置時照樣佔滿頻寬。
  這個專案的 burst 設計是刻意的。
- **Hard-decision Viterbi + RS**：這個專案的 Turbo r1/3 在正確的 soft 輸入下強很多，
  這次的改動已經證明了這點。

## 還可以只改 RX 的地方（下一步）

這些不必改 wire format，可以先做：

- **Header 解出來之後，把它當成額外的 training**：header 的 3 個 OFDM symbol 一旦解出來，內容就是已知的。
  用 training 加上 3 個 header symbol 一起估 channel，估計誤差約再降 6 dB。
  預期 payload 門檻再降約 0.5–1 dB，multipath 下最明顯。
- **Payload 失敗時用 decision-directed 重估 channel 再解一次**：要多花一次 Turbo decode 的 CPU。

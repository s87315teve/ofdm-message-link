# 雙向 FDD：`node_app`、link adaptation 與實機量測

[Wire v5](../tx_v5/README.md)（`tx-v5`）之前，這個專案只有單向的 link：`tx_app` 發、`rx_app` 收。
這個資料夾記錄 `duplex-fdd` 分支怎麼把它變成**雙向**：一台 Pluto 一個 `node_app` process，同時在
一個頻率發、在另一個頻率收（FDD），每個 burst 帶回 feedback，讓對方的 link adaptation（LA）
為每個 burst 選 MCS。也記錄量測用的腳本與數據。

**語意仍是 UDP。** 掉了的 datagram 不會重送；ARQ 只寫了提案（第 5 節），沒有實作。
**`tx_app` 與 `rx_app` 不變。** 沒有設定 hook 時，它們送出的 burst 與以前 bit 相同（golden-hash
測試），收的東西也一樣。

表裡的數字分四種，每張表都會標明：

- **實測**：兩台 Pluto 的即時 run 或錄音，有結果檔。
- **模擬**：測試或 review script 在假的 link 上的輸出。只記在 commit message 裡的會註明「commit 記錄」。
- **預測**：由模型或設計算出來的，沒有量。
- **初步**：硬體量測還在進行，數字是從已完成的 run 算出來的，WP-A 的 `results/<run>/README.md`
  完成後以它為準。

兩台 ADALM-Pluto（韌體 v0.39）：`…1e6e77` = node 1 = A，`…1ef9c6` = node 2 = B。
A→B 是 node 1 發、node 2 收（2420 MHz），B→A 相反（2460 MHz）。

## 1. 結論

### Owner 的要求

| # | 要求 | 結果 | 依據 |
|---|---|---|---|
| 1 | FDD，2.420／2.460 GHz | **完成**。Node 1 TX 2420／RX 2460 MHz，node 2 相反；`configs/profiles/duplex_fdd_2420_2460.yaml`。備用 2405／2475 | 2.1、4.2 |
| 2 | 先量自干擾（SI）再決定頻率 | **完成**（M1，實測）。S = 20／40／70 MHz、RX gain 15／30 dB 都量不到 SI：data carrier 的底噪上升 ≤ 0.15 dB，peer 的 SNR 下降 ≤ 0.6 dB。決定 FDD 2420／2460、RX gain 15 dB、analog BW 5 MHz，不需要 LBT | 2.1、4.2 |
| 3 | 一台 Pluto 一個 process（`node_app`），有 GUI 也有 headless | **完成**。TX 與 RX 在同一個 engine 子 process，共用一個 libiio context；`--headless` 給腳本，視窗模式給 demo | 2.2 |
| 4 | UDP ports | **完成**。`base = 52200 + 10·node_id`：node 1 ingress／egress／control／UDP sample = 52211／52212／52213／52214，node 2 = 52221–52224。不設 `SO_REUSEADDR`，衝突時 EADDRINUSE | 2.2 |
| 5 | `tx_app`／`rx_app` 照常可用 | **完成**。Hook 預設 `None`；golden-hash 測試確認沒有 policy 時 burst 不變；`rx_app` 收 node 的 burst 時 CONTROL frame 只記在 `control_frames` | 2.4 |
| 6 | 雙向 datagram | **完成**（UDP transport：`test_node_udp_duplex.py` 兩個方向逐 byte 正確）；**實測**：MCS 12、兩個方向各 3.24 Mbit/s 同時跑，A→B 0 遺失、B→A 0.86%（初步，4.3） | 4.3 |
| 7a | 可替換的 LA：Protocol 介面 | **完成**。`LinkAdaptation`、`FeedbackReporter` 是 `typing.Protocol`，不需要繼承 | 2.3 |
| 7b | `module:Class` loader | **完成**。`fixed`／`olla`／`arf`、`package.module:Class`、`path/file.py:Class`；`--link-adaptation-arg K=V` 以 Python literal 解析；錯誤一行 `SystemExit` | 2.3 |
| 7c | 可替換的 reporter | **完成**。`--feedback-reporter SPEC`，內建 `default` | 2.3 |
| 7d | Feedback piggyback，有 version 欄位 | **完成**。每個 burst 的第一個 MPDU 是 CONTROL frame（LCP header 有 `version`，其他版本被忽略並計數） | 2.4 |
| 7e | 閒置時送 control burst | **完成**。沒有 data 時送 control-only burst（MCS 8，約 0.48 ms） | 2.4 |
| 7f | 預設是兩個 loop 的 OLLA，target 1% | **演算法完成，但命令列預設不是它。** 內建的預設演算法是 `olla.OllaLinkAdaptation`（inner loop + outer loop，`target_loss=0.01`）；`node_app` 的 `--link-adaptation` 預設目前是 `fixed`（固定在 `--mcs-index`），要 LA 必須給 `--link-adaptation olla`。量測腳本都明確給 `olla`。要改預設，只要改 `node_options.DEFAULT_LINK_ADAPTATION`（這份文件沒有改程式） | 2.3 |
| 8 | 單元測試 | **完成**。LA、control wire、link hooks、node 的每一部分都有測試（6 節）；合併 `tx-v5` 時全部 1577 個測試通過（2 個已知的環境失敗） | 6 |
| 9 | ARQ 只提案 | **完成**：LTE RLC AM 式的提案，沒有實作 | 5 |

### 其他結論

- **限制是 USB 與 CPU，不是 RF。** 一台 Pluto 同時以 5 MS/s 接收時，USB 2.0 只讓 TX 推到 duty
  約 0.78；`…1e6e77` 在這個上限每分鐘 overflow 177–224 次，`…1ef9c6` 不會（M0，實測）。所以
  `node_app --tx-duty-cap` 預設 0.7。
- **同一台 Pluto 開第二個 libiio context 會 EBUSY**（實測），所以 TX 與 RX 共用
  `pluto.SharedPlutoContext`，兩個方向在同一個 process。
- **路徑不對稱約 6 dB**：同樣的 SNR，B→A 要比 A→B 多約 6 dB 的 TX gain（M1 的校準、M2 smoke）。
  M2 的 matrix 因此讓 node 2 的 gain 比 node 1 高（`--m2-gain2-offset`）。
- **第一次 M2 smoke 失敗的原因是 RX acquisition 的 CPU**：Pluto 的 LO 洩漏是 DC，舊的
  acquisition 在 DC 或 CW 下每個 offset 都進入 coherent 階段，RX owner 單獨就吃 0.88 core 而
  overflow。修完（`7006eb6`）之後，同樣的 smoke 0 overflow，A→B 遺失 10.8% → 0（2.5、4.3）。
- **雙向不比單向多掉（M2，初步）。** MCS 12 的三個 gain，同一個方向雙向與單向的遺失差
  ≤ 0.7 個百分點、SNR 差 ≤ 0.2 dB（MCS 8 的差在 TX 端）。但**雙向的延遲高 2–6 倍**（MCS 12 p50 單向 77–107 ms、雙向
  91–637 ms），原因沒有查。
- **Node 的 MCS 8 容量比 `tx_app` 低**：180 datagram/s（1.30 Mbit/s）送不完，只到 1.16–1.22 Mbit/s，
  而 `tx_v5` 第一輪的 `tx_app` 有 1.748（4.3，原因沒有查）。
- **審查找到 7 個 LA／link 的 bug，全部修了**，每個都有先失敗後通過的測試（2.5）。其中 OLLA 的
  per-report clamp 讓 loss 的平衡點從 1% 跑到約 4%（每份 report 16 個 subframe）或 9%（40 個）。
- M3（LA 跟著 gain 變化）與 M4（雙向影像）：【待填：M3】【待填：M4】

## 2. 做法

### 2.1 自干擾量測與頻率決定（M0、M1）

**為什麼先量。** FDD 的 node 一邊發一邊收，自己的 TX 可能用三種 RF 機制干擾自己的 RX，另外還有 USB；各有不同的解法：

| 症狀 | 機制 | 有用的 | 沒用的 |
|---|---|---|---|
| 自己的 TX gain 每加 1 dB，data carrier 的底噪也加約 1 dB，與 RX gain 無關 | TX 的寬頻雜訊落在 RX 頻帶 | 加大頻率間距、antenna 分開 | RX gain、analog BW |
| 底噪或 clipping 隨 RX gain 急升，RSSI 高 | LNA／mixer／ADC 壓縮 | 降低 RX gain、縮窄 BW | — |
| 少數幾個 bin 有線 | LO 洩漏、spur、aliasing | 換間距 | gain |
| Overflow 隨自己的 TX duty 增加，連 TX −89.75 dB 也一樣 | USB 2.0 頻寬（不是 RF） | kernel buffers、TX duty 上限 | 任何 RF 的設定 |

**M0：USB（`usb_capacity.py`）。** TX −89.75 dB（幾乎沒有 RF），每台 Pluto 在共用的 context 上以
5 MS/s 接收，同時自己的 TX 以 duty 0／0.5／0.6／0.7／0.8／0.9 推 6.9 ms 的 burst。發現：

- **第二個 context → EBUSY。** `--probe-double-open`：在一台正在 streaming 的 Pluto 上開第二個
  `IioContext`，回 `[Errno 16] Device or resource busy`。所以 node 必須共用一個 context。
- **TX duty 的上限約 0.78。** 要求 0.8 或 0.9，兩台都只推到 0.78–0.79：RX 20 MB/s 加 TX 約
  16 MB/s 是一台 Pluto 的 USB 2.0 的上限。
- **`…1e6e77` 在上限 overflow，`…1ef9c6` 不會。** 每分鐘 222–224 次（RX 只剩 4.78 MS/s）。設定與韌體
  相同；差別在裝置或 port（`…1e6e77` 在 root port，`…1ef9c6` 在 hub 後面，兩台與 webcam 共用
  bus 3 的 480 Mbit/s）。8 個 kernel buffers 沒有幫助（177／183 次）：這是頻寬，不是延遲。
- **Duty ≤ 0.7 兩台都乾淨**，單獨或同時。**決定：`--tx-duty-cap` 預設 0.7**
  （`node_engine.DutyCappedSink`：一個 burst 最早在前一個開始時間 + airtime ÷ 0.7 之後開始，閒置時
  最多累積 50 ms 的 credit）。

**M1：SI（`si_scan.py`）。** 一個 process 同時控制兩台 Pluto。DUT 的 RX 在 `f_rx`，自己的 TX 在
`f_tx` 送真的 v5 MCS 14 aggregate（6.85 ms），duty 0.7（設計是 0.9，但 M0 說 0.9 做不到，而且會讓
一台 overflow）。Peer 在 `f_rx` 送 MCS 8、一個 900 B datagram、每秒 100 個，先在自己 TX 關閉時校準到
preamble SNR 約 15 dB（mid）與約 10 dB（edge）。每個 phase 2 秒，共 20 分鐘：

| Phase | Peer | 自己的 TX | 量什麼 |
|---|---|---|---|
| a | 關 | 沒開 | 環境底噪；Wi-Fi 佔用 = 1 ms block 高於底噪 6 dB 的比例 |
| b0 | 關 | 開、−5 dB、閒置 | LO 洩漏 + TX 雜訊 |
| b1–b4 | 關 | burst，−20／−10／−5／0 dB | 底噪 vs. 自己的 gain、clipping、RSSI、overflow |
| b5 | 關 | burst，duty 0.3，−5 dB | 與 duty 的關係 |
| c-mid、c-edge | 開 | 沒開 | 基準 SNR 與遺失 |
| d-mid、d-edge | 開 | burst，−5 與 0 dB | ΔSNR、Δ遺失 |

Grid：間距 S = 20（2430／2450）、**40（2420／2460）**、70 MHz（2405／2475）× 兩台都當 DUT × RX gain
15／30／45 dB，analog BW 5 MHz；另外 S = 40、g = 15 時 BW 4 與 8 MHz；以及 S = 0（兩台都 2440 MHz，給
LBT 用）。

**判定規則**（設計 §1.3，`summarize` 算）：自己的 TX −5 dB、兩台 DUT 都要 PASS：底噪上升 ΔN ≤ 1.0 dB、
ΔSNR ≤ 1.0 dB、Δ遺失 ≤ 0.5 個百分點、clipping < 1e-6、Δoverflow ≤ 0.5／分鐘。依序：S = 40、g = 15、
BW 5 PASS → FDD 2420／2460；否則換 g、BW；再否則 70 MHz；再否則 MARGINAL；最後才是單頻 + LBT。

**結果（實測，4.2）：第一步就 PASS。** 在 20、40、70 MHz、RX gain 15 與 30 dB：

- ΔN 都在 ±0.15 dB 內，與自己的 TX gain 無關（斜率 −0.04…+0.03 dB/dB；TX 寬頻雜訊會是約 +1），
  閒置的 TX、duty 0.3 與 0.7 都一樣。
- ΔSNR ≤ 0.6 dB，大多 ≤ 0.2 dB，正負都有。
- 沒有 spur：guard bin 最大的線在各 phase 之間不變。ADC 以 40 MS/s 取樣，所以 40 MHz 外的 TX 會
  alias 到 DC，但 analog filter 把它壓在底噪下（P_dc 不變）。
- Δoverflow 全部是 0（duty 0.7）。

**表裡的 FAIL 不是 SI。** 每個 FAIL 都來自 Δ遺失，而 Δ遺失是計數雜訊加 Wi-Fi：200 個 burst 裡的
1 個就是 0.5 個百分點，edge phase 在 10 dB SNR。同樣的組合 ΔN ≈ 0、ΔSNR ≈ 0。0.5 個百分點的規則
每個 phase 要 ≥ 2000 個 burst 才有意義，所以遺失留給 M2–M4 的 60 秒 run 量。RX gain 45 dB 會 clip
（peer 在 mid 時 clip 比例 1e-4 到 3.9e-2），是 RX gain 的問題，不是自己的 TX。

**Wi-Fi。** 2420 在 Wi-Fi channel 1（2401–2423 MHz）、2460 在 channel 11（2451–2473 MHz）。Phase a
的佔用（g = 15／30／45）：2420 MHz 0／2.6／20%，2460 MHz 1.6／7.8／29%。LA 把它們的 burst 看成
channel loss；2430 MHz 有一個高於底噪 14–28 dB 的環境訊號，所以 20 MHz 間距只當趨勢點。

**決定：FDD 2420／2460 MHz、RX gain 15 dB、RX analog BW 5 MHz；不需要 LBT。** g = 15 dB 時 peer 在
TX gain −8 dB（A←B）／−14 dB（B←A）就有 14–15 dB SNR，還有 15–20 dB 的餘裕。LBT 的設計（設計稿 §5）
沒有實作。

### 2.2 `node_app` 架構

```
node_app parent（視窗 或 headless）       node engine 子 process（_node_main，switch interval 2 ms）
  stats log、control socket、              SharedPlutoContext ─ PlutoSampleSource（refill thread）
  gain profile                             │                  └ PlutoSampleSink ← DutyCappedSink（0.7）
  ⇅ _EngineProcess 的 queues               ReceiveWorker ─ MessageReceiver ─ SegmentDecodePool（3 個 process）
                                           TransmitWorker ─ MessageTransmitter；IngressListener；egress
                                           LinkController（LA + reporter + codec，一把 lock）
```

- **共用的 Pluto context**（`pluto.SharedPlutoContext`）。用 reference count 把一個 libiio context
  租給 sink 與 source；`PlutoSampleSink`／`PlutoSampleSource` 本身不變，只是拿到
  `open_context=shared.factory()`。Attribute 與 register 的呼叫經過一把 lock（libiio 0.x 沒有保證
  兩個 thread 可以同時用一個 context 的 control pipe）；buffer 的 refill 與 push 不鎖（各自的 USB
  pipe，ctypes 會放開 GIL）。Sample rate 是兩個方向共用的 AD936x 設定，`configure()` 只寫一次；
  之後 `_tune` 再寫同樣的值時不碰裝置，寫不同的值會被拒絕（重寫可能讓 clock chain 在 stream
  進行中重跑）。`PlutoSettings.rf_bandwidth` 是新的 RX analog filter 設定；RX kernel buffer 數在
  建立 RX buffer 之前設。
- **啟動與停止順序。** TX 先開、先停，所以 context 關掉之前 TX attenuator 已經回到最小輸出。
- **同一個 engine 子 process 跑兩個方向**（`node_engine.py`），重用單向 app 的
  `TransmitWorker`、`ReceiveWorker`、decode pool、UDP ingress／egress 與 `engine._serve`，
  `engine.py` 沒有改。`LinkController` 同時是 TX worker 的 burst policy、receiver 的 control handler、
  RX worker 的 observer。設計稿另有一個 `split` layout（radio／RX 與 TX 分兩個子 process）作為
  GIL 不夠用時的備案，沒有實作。設計的觸發條件之一是 start-up 之後 RX overflow > 1 次／分鐘：RX
  修正之後 smoke2 是 0，但 MCS 12 雙向 60 秒的 run 在 node 1 有 3 次（4.3），已經碰到這條線；要不要做
  看 M2 的完整結果。
- **`--transport udp`**：兩個 node 在同一台 PC 上把 samples 送到對方的 UDP sample port，不需要 radio
  就是一條完整的雙向 link。
- **CPU 預算。** 兩個 node 在同一台 PC，所以 `node_app` 的 decode workers 預設 3（`rx_app` 仍是 4）。
- **Headless**：每 0.5 s 寫一行 stats（`tx`、`rx`、`la`、`feedback`、`cpu`），依 `--gain-profile`
  準時改 TX gain，提供 localhost 的 JSON control socket（`set_gain` 回 readback、`set_mcs`、`set_la`、
  `mark`、`stats`、`stop`）。SIGTERM 或 `--duration` 到時先停 TX 再停 RX、刪除 ready file，exit 0；
  radio 啟動失敗 exit 2，engine 子 process 中途結束 exit 3。
- **視窗**（`node_window.py`）：TX、RX、LA 三排 cards，60 秒的 MCS／filtered SNR 軌跡，auto／fixed
  切換。見 [docs/02 2.8](../../docs/02-gui.md#28-雙向-node-的視窗node_app)。
- **Ports**：見 1 節第 4 項；完整命令列見 [docs/07 7.9](../../docs/07-reference.md#79-node_app雙向-node-的命令列)。
- **設定**：`LinkConfig.duplex`（選用）。載入時驗證：剛好兩個 node、id 不重複、一個 node 的 TX 頻率
  是另一個的 RX 頻率、FDD 間距至少一個 sample rate；`tx_app`／`rx_app` 忽略這一段。

### 2.3 Link adaptation 的 interface

套件 [`src/ofdm_message_link/la/`](../../src/ofdm_message_link/la/)。怎麼寫自己的模組，完整說明與可以跑的
範例在 [docs/03 3.4](../../docs/03-your-own-app.md#34-寫你自己的-link-adaptation-模組)；這裡只列重點。

**兩個 Protocol**（`la/api.py`，不需要繼承任何 class）：

| Protocol | 在哪一端 | Methods |
|---|---|---|
| `LinkAdaptation` | TX | `on_sent(SentBurst)`、`on_feedback(FeedbackReport)`、`decide(now_s) -> Decision`（每個 data burst 一次，要快）、`snapshot() -> dict`；選用的 `on_mode("fixed" \| "auto")` |
| `FeedbackReporter` | RX | 屬性 `reporter_id`；`on_preambles(now_s, snr_db, *, control_only)`、`on_burst(BurstReport)`、`on_rx_health(now_s, RxHealth)`、`due(now_s)`、`next_due_s(now_s)`、`build(now_s) -> FeedbackReport` |

**資料結構**（frozen dataclass）：`BurstReport`（一個收到的 DATA burst：時間、第一個 sequence、MCS、
preamble SNR、ok／lost subframe 數）、`FeedbackReport`（從 wire 解出的 report，含累計計數器、
最近的 bursts、TLV `extras`、本地收到時間 `received_t_s`、`session_id`）、`SentBurst`、
`Decision(mcs_index, reason, snr_eff_db, offset_db)`、`LaContext`（`ladder`、`thresholds_db`、
`initial_mcs_index`、`sample_rate`、`node_id`）、`ReporterContext`、`RxHealth`。工具：
`counter_delta`（u32 wrap 的差）、`is_newer_seq`（u16 的新舊）、`FeedbackTracker`（處理重複、
peer 重新啟動、長時間的空檔，回傳計數器的增量；自訂演算法應該用它）。

**Loader**（`la/loader.py`）：

| `SPEC` | 意義 |
|---|---|
| `fixed` | 固定在 `--mcs-index`（`node_app` 目前的預設） |
| `olla` | 內建預設演算法：兩個 loop 的 OLLA，target 1% |
| `arf` | 內建範例：Auto Rate Fallback（`la/examples.py`，不到一百行，可當範本） |
| `package.module:Class` | 可以 import 的 module |
| `path/to/file.py:Class` | 一個檔案，不用安裝 |

`--link-adaptation-arg K=V`（可重複）以 `ast.literal_eval` 解析，失敗就當字串；同一個 K 兩次或少了
`=` 會停止。建立方式是 `Class(context, **kwargs)`，結果必須通過 Protocol 的 `isinstance` 檢查。
所有錯誤都是一行 `SystemExit`。Parent 先建一個用完就丟的 instance 驗證 spec；子 process 再用 spec
字串建真的那個，所以不需要 pickle class。Reporter 一樣：`--feedback-reporter SPEC`、
`--feedback-reporter-arg K=V`，內建 `default`。

**Controller**（`la/controller.LinkController`）把演算法接到 link layer：每個 burst 前問
`decide`，同一個 entry 用在 aggregation 的規劃與 encode（MCS 改變不會讓 burst 超過 airtime 上限）；
所有呼叫在一把 lock 下，所以演算法與 reporter 不必 thread-safe。演算法或 reporter 丟例外時，計數
（`la_errors`、`reporter_errors`）並用安全的預設值（目前的 MCS、不送 report），不會讓 TX 或 RX
thread 停下來。

**預設演算法 `olla.OllaLinkAdaptation`**（LTE 式，兩個 loop）：

- **Inner loop**：filtered peer SNR（EWMA，τ = 0.1 s，只用有 preamble 的 report）+ offset = SNR_eff，
  選 threshold 不超過 SNR_eff 的最高 rung。升階要 T_new + 1 dB，連續 2 份**帶新 SNR** 的 report，
  而且距上次改變 ≥ 0.1 s；降階立即。
- **Outer loop**：每個 peer 收到的 DATA subframe offset +Δup，每個遺失的 −Δdown。Δdown = 0.5 dB、
  Δup = Δdown·t／(1−t) = 0.00505 dB，所以 offset 不再漂移時 loss = Δup／(Δup+Δdown) = t = 1%。
  （沒有 HARQ 可以救回遺失，所以不用 LTE 的 10%。）一份 report 的整個更新乘上
  `gain = min(1, n_cap／n_avg)`，`n_cap = max_report_down_db／Δdown = 4`，`n_avg` 是每份 report
  的 subframe 數的移動平均（2.5 第 6 項）。Offset 限制在 −10…+3 dB。
- **Stale**：0.5 s 沒有收到 report，或 report 一直來但 0.5 s 前送出的 burst 都沒有被聽到（forward
  link 斷了），每 0.5 s 降一階，最低 MCS 8。
- RX overflow 計數改變的 report 不更新 outer loop（那是 CPU 造成的遺失，不是 channel）。
- Fixed mode 時凍結 outer loop，回到 auto 時沿用原來的 offset。

| 參數 | 預設 | 說明 |
|---|---:|---|
| `target_loss` | 0.01 | DATA subframe 的遺失目標 |
| `step_down_db`／`step_up_db` | 0.5／0.00505 | `step_up` 預設由 target 算出 |
| `max_report_down_db`／`max_report_up_db` | 2.0／0.5 | 一份典型大小、全部遺失的 report 讓 offset 立即降 2 dB；硬上限 −4／+0.5 dB 只擋異常值 |
| `offset_min_db`／`offset_max_db` | −10／+3 | |
| `snr_tau_s` | 0.10 | |
| `hysteresis_up_db`／`hysteresis_down_db` | 1.0／0.0 | |
| `up_hold_reports`／`min_dwell_s` | 2／0.10 | |
| `stale_after_s`／`stale_step_s`／`fallback_mcs` | 0.5／0.5／8 | |
| `skip_outer_on_overflow` | True | |
| `thresholds` | `configs/la/thresholds_pluto_v5.yaml` | 可以給另一個 YAML |

**Ladder 與 threshold。** `configs/la/thresholds_pluto_v5.yaml` 是 GUI 燈號與 LA inner loop 共用的
**唯一**一張表（wire v5 實測：遺失 < 1% 的 SNR + 1 dB，見 [tx_v5 4.1](../tx_v5/README.md#41-錄音每個-mcs-遺失--1-的最低-tx-gain實測)）。
Ladder 依 threshold 由低到高走，只留比前面每一個都快的 entry：

| MCS | 8 | 9 | 10 | 12 | 13 | ~~14~~ | 15 | 16 | 17 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Threshold（dB） | 7.5 | 9.0 | 11.0 | 14.0 | 15.5 | 17.5 | 16.5 | 21.0 | 22.0 |
| bit/symbol | 0.67 | 1.0 | 1.33 | 2.0 | 2.67 | 3.0 | 3.0 | 4.0 | 4.5 |

**MCS 14 被拿掉**：它和 MCS 15 一樣是 3 bit/symbol，卻多需要 1 dB（dominated）。MCS 11 沒有量，表裡
沒有值，所以也不在 ladder 上。Wheel 安裝時 `setup.py` 把這個檔複製進套件（`530ebce`）。

**預設 reporter `la/reporter.DefaultReporter`**（`period_ms=20`、`keepalive_ms=200`、`max_entries=8`）：
量測窗內所有 peer preamble 的 SNR（mean、p10、last）、累計的 DATA ok／lost、RX 的健康計數
（clipping、decode backlog：窗內任何一個 chunk 有就設）、最近的 DATA bursts。Report **due** =
（上次之後收到新的 peer DATA burst **而且**過了 20 ms）**或**過了 200 ms（keepalive，讓 peer 分得出
安靜與斷線）。Peer 的 control-only burst 只更新 SNR，不會讓 report due，否則兩個閒置的 node 會
互相觸發。每個 reporter 啟動時抽一個隨機的 32-bit session id。

**換成你自己的**：寫一個有四個 method 的 class，`--link-adaptation my_la.py:MyClass
--link-adaptation-arg k=v`。範例、規則（`decide` 要快、計數器是累計的、第一份 report 只當基準、
不需要 lock）與不開 radio 的試法見 [docs/03 3.4](../../docs/03-your-own-app.md#34-寫你自己的-link-adaptation-模組)。

### 2.4 Wire format 摘要

完整的欄位表在 [docs/07 7.8](../../docs/07-reference.md#78-雙向-link-的-control-與-feedback-wire-format)，
codec 在 [`control.py`](../../src/ofdm_message_link/control.py)。

- **位置。** Node 的每個 v5 burst 的**第一個 MPDU** 是 `FrameKind.CONTROL` frame，payload 是一個
  Link Control PDU（LCP）。它的 sequence 來自**獨立的** 16-bit control counter，所以 DATA 的 sequence
  保持連續，遺失的計數不受影響，舊的 `rx_app` 也不會看到假的 gap。
- **LCP header（8 bytes）**：magic `0x4C43`、**version**（1；其他版本整個忽略並計入
  `control_unsupported`）、pdu_type（1 LINK_HEADER、2 FEEDBACK、3 STATUS 保留給 ARQ）、src_node、
  dst_node、flags、body_length。
- **FEEDBACK body v1**：36 bytes 的固定部分 + 每個 6 bytes 的 burst entries + TLV。計數器是**累計**的
  （u32），掉了幾份 feedback 也不會遺失資訊。TLV 0x10 是 reporter 的 session id（peer 重新啟動時
  立即重設基準）；0x80–0xFF 給自訂 reporter。比已知長的 body 照收、忽略尾巴，所以以後可以加欄位。
- **成本**：LINK_HEADER 每個 burst 24 bytes（含 delimiter 與 padding；一個 900 B datagram 的 2.7%，
  8 KB aggregate 的 0.3%）；4 個 entries 的 FEEDBACK 84 bytes。
- **Control-only burst**：沒有 data 時，MCS 8、只有 CONTROL frame；FEEDBACK 帶 4 個 entries 時約 2400 samples（0.48 ms，預測）。
- **Robust copy**：data 的 MCS 高於 9 時，每 100 ms 至少有一份 feedback 走 MCS ≤ 9 的 burst，
  data 的 MCS 太高時 feedback 仍然到得了。
- **自己的洩漏。** 兩個 node 用不同的 scrambler seed：node 1 用 1–63、node 2 用 64–127
  （`link.node_seed_range`；`tx_app` 仍用 1–127）。RX 由 SIG 的 seed 在 payload decode 之前就丟掉
  自己的 burst（省 Turbo 的 CPU），CONTROL frame 的 src_node 是第二道檢查。
- **解碼端不會因為格式錯誤而失敗**：`decode_lcp`／`decode_feedback` 遇到讀不懂的東西回傳 `None`。

### 2.5 審查之後修的問題

LA 與 link layer 合併後，一個只讀的 reviewer 用 review script 在假的 link 上找到 7 個 bug。全部修了，
每個修正都附了在修正前的 commit（`f8c6d0b`）會失敗、修正後通過的測試。數字是 review script 或測試
的輸出（模擬，commit 記錄）。

| # | 問題 | 修法 | 量到 | Commit |
|---|---|---|---|---|
| 1 | Peer 重新啟動後 `report_seq` 從 0 開始，「不是較新的 report 就忽略」讓 LA 忽略它最多 32767 份（20 ms 一份約 11 分鐘），期間 stale 降到最低 | `la.api.FeedbackTracker` 統一處理：只忽略遲到或重複的；session id 改變、距上一份超過 `stale_after_s`、或 u32 計數器倒退時重設基準。Session id 走 TLV 0x10，v1 的固定格式不變 | 修正前 2000 份被忽略、重啟 40 s 後仍是 MCS 8 stale、ARF 在大量遺失下卡在 MCS 17；修正後 0 份被忽略 | `ec50286` |
| 2 | 自己的 burst 只有在 CONTROL frame 解得出來時才被認出；一個 CONTROL CRC 失敗的自己的 burst 進了 DATA sequence tracker，一次多出約 20 000 個「遺失」 | 三層：SIG 的 seed 落在自己的範圍就整個丟掉（`own_seed_range`）；有 handler 時，sequence 跳超過 1024 要下一個 sequence 確認才重新同步，不算遺失；`BurstReport.subframes_lost` 也有同樣的上限 | 修正前 lost 19 997；修正後 0 | `6bb5988`、`5bb7583` |
| 3 | Burst policy（例如有 bug 的自訂 LA）丟例外時，TX worker 立刻重跑迴圈：busy-spin 而且什麼都沒送 | 每個 policy 呼叫都有 guard：例外時用 transmitter 目前的 entry、不帶 CONTROL，data 繼續送；錯誤計數，每個 method 每秒最多報一次；失敗後退讓 50 ms | 修正前 0.3 s 內 217 260 次錯誤、0 個 burst；修正後 1 次錯誤、burst 送出、4 ms CPU | `32d5ed2` |
| 4 | Control handler（實際上是自訂 LA 的 `on_feedback`）的例外穿出 `MessageReceiver.feed`：CONTROL 後面的 DATA 沒有交付，`rx_app` 丟掉整個 chunk | Handler 丟例外當作 ACCEPT，計入 `control_handler_errors`；controller 對演算法、reporter、encoder、LA log、`on_event` 的每個呼叫都加 guard | 修正前 `feed raised ZeroDivisionError`、0 個訊息交付；修正後訊息交付 | `fa1d4bb` |
| 5 | RX 把**所有** preamble 的 SNR 交給 reporter，包括自己 TX 的洩漏（之後才被當作自己的 burst 丟掉）；peer 的 LA 把很強的自己的 preamble 當成 channel，選了太高的 MCS | `MessageReceiver.drain_peer_snr_db()` 只回傳被接受的 burst 的 SNR；有 observer 時 reporter 只拿這些 | 修正前 report 寫 preambles = 2、SNR 42.9 dB（自己的 46.6 dB 混進去）；修正後 preambles = 1、peer 的 33.1 dB | `b882924` |
| 6 | OLLA 先加總一份 report 的每個 subframe step，再 clamp 到 [−2, +0.5] dB。一個 aggregate 的 subframe 一起掉，所以 clamp 只截掉壞的 report，好的 report 拿到完整的 +Δup·n：平衡點從 1% 跑到約 4%（每份 16 個）或 9%（40 個） | 仍然每個 subframe 一個 step，但整份 report 乘上只與 report 大小有關的 gain = min(1, n_cap／n_avg)，好壞一樣縮放，所以平衡點剛好在 target；硬 clamp −4／+0.5 dB 只擋異常值 | 20 000 份 report、整窗遺失，在 0.5／1／2% 遺失時的 offset 漂移（dB）：修正前 N = 16 +1344／+1148／+715、N = 40 +3752／+3545／+3087（在 2% 仍往上，平衡點高於 2%）；修正後 N = 16 與 40 都是 +139／−51／−471（在 0.5% 與 2% 之間變號） | `3ef16aa` |
| 7 | Reporter 只把最後一個 chunk 的 clipping／decode backlog 放進 report；20 ms 的窗約兩個 chunk，第一個 chunk clip 的話 report 說沒有 | 窗內每個 `RxHealth` 取 OR，`build()` 時清除 | 修正前 flags 0；修正後兩個 bit 都設，下一個窗清除 | `2d392d2` |

同一輪另外修的（不在 reviewer 的 7 個之內）：

- **Forward link 斷了但 feedback 還在來**時 OLLA 永遠不降階；只帶 keepalive（沒有 preamble）的 report
  也算進升階的連續次數（review script R4：MCS 8 → 15）。現在兩者都處理（`f0a718c`）。
- **Fixed mode 時 OLLA 仍在學**：試 MCS 17 在 18 dB 兩秒後回到 auto，offset −9.8 dB、MCS 8，要 0.9 s
  才爬回來；現在凍結，回到 auto 時是 MCS 15、offset 0.59 dB（`69d665c`）。
- 閒置的 TX worker 最多睡 100 ms，收到 peer 的 DATA burst 之後 feedback 要等這麼久；現在最多睡
  reporter 的 period（20 ms）（`e6c6a31`）。
- 其他版本的 LCP 原本算成 `control_undecodable`，現在算 `control_unsupported`（`1288128`）。
- 非 editable 安裝找不到 threshold 檔（`530ebce`）。

**解開 M2 的 RX 修正**（`7006eb6`，在 `tx-v5` 上修、合併進來）。第一次 M2 smoke（4.3）兩個方向都
overflow：每個 engine 子 process 在 load 之前就約 1 core。Pluto 上只收不發、沒有 burst，RX owner
就用 0.88 core、8 秒 7 次 overflow。離線：乾淨雜訊 0.17 core；DC offset 與雜訊一樣大時 0.81 core，
3 倍時 9 core；CW tone 相同（0.98、9.4）。時間都在 `sync._acquisition_candidates` 與
`sync_v5.v5_candidates`。Pluto 的 LO 洩漏就是這樣的 DC。原因與修法：

- DC 或 tone 讓 legacy 的 metric 在每個 offset 都超過門檻（legacy 的 gate 沒有去掉穩定的部分），
  每個 offset 都進入 coherent 階段；v5 的 gate 雖然減掉穩定的 product，干擾 × 雜訊的交叉項仍超過
  0.10。兩個 gate 現在都去掉穩定的部分，只在干擾在兩側都顯著（4σ）的地方做 normalize，其他地方
  修正剛好是 0，所以乾淨的 buffer 判斷 bit 相同。
- 其他：CFO phasor 分兩段建、drain 只在有新 sample 時做、decode worker 保留釋放的記憶體、送進 pool
  一次最多 4 個、v5 detector 少掃幾次。

量到（`experiments/tx_v5/results/rx_rate/summary.txt`，owner = 接收執行緒佔一個 core 的比例）：空
stream 加 DC 0.003：556% → 24%；DC 0.01：884% → 38%；tone 0.003：552% → 24%；Pluto 式雜訊（DC 0.003、
guard +10 dB）每秒 300 個 burst：90% → 61%。`sim_acq.py` 修正前後輸出完全相同，legacy golden 不變。
修完之後的 M2 smoke：見 4.3。

### 2.6 決策紀錄

| 決定 | 依據 |
|---|---|
| FDD 2420／2460 MHz，RX gain 15 dB，analog BW 5 MHz | M1：40 MHz、g = 15、BW 5 兩台 DUT 都 PASS；20／40／70 MHz 都量不到 SI |
| 不做 LBT | M1 第一步就 PASS |
| 一個共用的 libiio context，兩個方向在一個 process | M0：第二個 context EBUSY |
| `--tx-duty-cap` 預設 0.7 | M0：要求 ≥ 0.8 時只到 0.78，`…1e6e77` 每分鐘 overflow 177–224 次；≤ 0.7 兩台都乾淨 |
| Decode workers 預設 3 | 兩個 node 在同一台 PC 的 CPU 預算（設計 §2.3） |
| Ports 52211–52224，不設 `SO_REUSEADDR` | 不與 52001／52002／52012／52022／52101 衝突；衝突時大聲失敗，不默默分流 |
| CONTROL frame 用獨立的 sequence counter | DATA sequence 連續，遺失計數乾淨，舊 `rx_app` 不看到假的 gap |
| Seed 範圍分給兩個 node（1–63／64–127） | SIG 的 13 個保留 bit 必須是 0（已部署的 RX），不能放 node id；seed 有 CRC-16 保護而且可以自由選 |
| 累計計數器 + session id | 掉幾份 feedback 不遺失資訊；peer 重新啟動立即重設基準 |
| OLLA target 1%（不是 LTE 的 10%） | 沒有 HARQ 救回遺失 |
| OLLA 用與大小有關的 gain，不用 per-report clamp | Clamp 讓平衡點跑到 4–9%（2.5 第 6 項） |
| Ladder 拿掉 MCS 14 | 與 MCS 15 同樣 3 bit/symbol，多需要 1 dB |
| Fixed mode 凍結 OLLA，不重設 offset | 操作員介入前學到的仍然有效 |
| `split` layout 沒有實作（待定） | RX 修正之後 smoke2 0 overflow；MCS 12 雙向有 3 次／60 秒，碰到設計的 1 次／分鐘的線，看 M2 完整結果再決定（4.3） |

## 3. 怎麼量的

所有 Python 都以 `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1` 執行。每個硬體 run 都：
`flock /tmp/ofdm_hw.lock`（一次一個重的工作）；1 分鐘 load average < 4 才開始（最多等 10 分鐘），每秒
記錄 load，超過 8 就把 run 標為無效；記錄 `lsusb -t`、git revision、serial、gain。兩台 Pluto 各接原廠
antenna，同一張桌上，與 tx_v5 相同的擺放；5 MS/s，RX gain 15 dB。

### M0 與 M1

```bash
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONPATH=src
ACK='I acknowledge that this process will transmit RF'
python experiments/duplex_fdd/usb_capacity.py run --out experiments/duplex_fdd/results/usb \
    --probe-double-open --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/usb_capacity.py run --out experiments/duplex_fdd/results/usb_duty \
    --parts per-pluto --duties 0.6,0.7,0.8 --seconds 30 --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/si_scan.py run --dut 104473541196000419000c00f2ff1e6e77 \
    --peer 10447354119600041b000b00f2ff1ef9c6 --out experiments/duplex_fdd/results/si/run1 \
    --own-duty 0.7 --enable-rf --acknowledgement "$ACK"
python experiments/duplex_fdd/si_scan.py summarize experiments/duplex_fdd/results/si/run1
```

- `usb_capacity.py`：每台 Pluto 在共用的 context 上收 5 MS/s，自己的 TX 依 duty 推 burst，記錄
  overflow／分鐘（前 3 秒的 start-up overflow 另外記）、實際 RX rate 與 TX duty。`--probe-double-open`
  試第二個 context。
- `si_scan.py`：2.1 的 phases 與 grid；每次 2 秒的錄音（int16）在兩台 TX 都在最低時立刻離線分析，
  不加 `--keep-raw` 就刪掉。分析：64 點 FFT 的每個 bin 的底噪（52 個 data carrier、guard、DC，dBFS）、
  有 peer burst 時把高於中位數 3 dB 的 block 遮掉；peer 的 preamble SNR 與遺失（`MessageReceiver`
  離線解）；clipping、RSSI；overflow。`summarize` 產生 `si_scan.json` 與 `si_summary.md`（判定表）。
  底噪的上升 ΔN 用遮掉 burst 的底噪算：沒有遮的底噪在兩次 2 秒的錄音之間因為 Wi-Fi 差 ±1 dB。

### M2–M4

- `run_duplex.sh`：啟動兩個真的 `node_app --headless`，每個方向一個
  `../ota_udp_throughput/udp_load.py`（固定速率、逐 byte 比對、延遲），記錄 `node{1,2}_stats.jsonl`、
  `la_{1,2}.jsonl`、`load_{AB,BA}.json`、`cpu.jsonl`（`cpu_multi.py`：每個 node 的 parent、engine 子
  process 與 decode workers）、`loadavg.log`、`meta.json`。只 kill 自己啟動的 PID。
- `run_matrix.sh fixed|la|all`：M2 與 M3 的 matrix（`--dry-run` 印出計畫）。
- `video_duplex.sh`：M4，經 `run_duplex.sh --hook` 啟動 node；A→B webcam、B→A `ffmpeg -f lavfi
  testsrc2`，H.264 CBR（與單向影像 demo 相同的設定，752-byte datagram），兩端各用 `ffmpeg -f null`
  消費；`video_counts.py` 數 frame 與 corrupt／error 行。
- `summarize_duplex.py RUN…`：每個方向、每個 segment（gain profile 的 step 之間；沒有 profile 時每
  30 秒）的 goodput、datagram 與 subframe 遺失、MCS 軌跡、SNR、offset、gain step 之後的收斂時間
  （之後每個 1 秒的窗遺失 < 2% 而且 2 秒內 MCS 不變）、RX overflow（前 3 秒以後）、CPU；寫
  `summary.json`、`summary.md` 與每個方向的 PNG。

| 量測 | 內容 |
|---|---|
| M2 固定 MCS | MCS 12 在 TX −5／−12.5／−20 dB，MCS 8 在 −20 dB；每點一次雙向與兩次單向對照（反方向閒置，只有 control burst），各 60 秒。900 B datagram，0.8 × 預測容量（MCS 12：每方向 450/s ≈ 3.2 Mbit/s；MCS 8：180/s）。Node 2 的 gain 比 node 1 高 `--m2-gain2-offset`（路徑不對稱），上限 0 dB |
| M3 LA 跟 gain 變化 | Node A `--gain-profile 0:-5,30:-20,60:-5`，ready 後 90 秒；node B −5 dB。A→B 4 Mbit/s（555/s），B→A 1 Mbit/s（139/s）。`olla` ×2；`olla` 而 B→A 閒置（feedback 只靠 control burst）×1；固定 MCS 8／10／12／15 各 ×1（ladder 上的 rung，15 取代 14）。與整個 run 與每個 segment 最好的固定 MCS 比較 |
| M4 雙向影像 | 兩個方向同時 60 秒，LA 開；H.264 750 kbit/s（M3 若在 −5 dB 有 MCS ≥ 10 就再跑 1300 kbit/s） |

```bash
bash experiments/duplex_fdd/run_matrix.sh fixed --out-root experiments/duplex_fdd/results/m2 --enable-rf
bash experiments/duplex_fdd/run_matrix.sh la --out-root experiments/duplex_fdd/results/m3 --enable-rf
bash experiments/duplex_fdd/video_duplex.sh --enable-rf --out experiments/duplex_fdd/results/m4/<run>
python experiments/duplex_fdd/summarize_duplex.py experiments/duplex_fdd/results/m2/*/
```

## 4. 數據

### 4.1 M0：USB（實測，`results/usb/`、`results/usb_duty/`）

每台 Pluto 收 5 MS/s，自己的 TX 在 −89.75 dB 推 6.9 ms 的 burst。每列 60 秒（0.6–0.8 為 30 秒）；
每一列前 3 秒的 start-up overflow 都是 0。

| 要求的 TX duty | `…1e6e77` overflow／分鐘 | `…1e6e77` 實際 duty | `…1ef9c6` overflow／分鐘 | `…1ef9c6` 實際 duty |
|---:|---:|---:|---:|---:|
| 0（sink 開著、閒置） | 0 | — | 0 | — |
| 0.5 | 0 | 0.50 | 0 | 0.50 |
| 0.6 | 0 | 0.60 | 0 | 0.60 |
| 0.7 | 0 | 0.70 | 0 | 0.70 |
| 0.8 | 222 | 0.78 | 0 | 0.79 |
| 0.9 | 224 | 0.78 | 0 | 0.79 |
| 0.9，兩台同時，4 個 kernel buffers | 183 | 0.78 | 0 | 0.77 |
| 0.9，兩台同時，8 個 kernel buffers | 177 | 0.78 | 0 | 0.77 |

兩台同時只收不發（30 秒）：0 overflow。第二個 context：`[Errno 16] ... Device or resource busy`。

### 4.2 M1：自干擾（實測，`results/si/run1/`）

自己的 TX −5 dB、duty 0.7，S = 40 MHz，RX gain 15 dB：

| BW | DUT | ΔN | ΔN，TX 0 dB（沒有遮） | ΔSNR | ΔSNR，TX 0 dB | 遺失 c-edge → d-edge | clip | Δoverflow／分鐘 | 判定 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---|
| 5 MHz | `…1e6e77`（RX 2460） | 0.00 dB | −0.36 dB | 0.43 dB | 0.13 dB | 4.0% → 2.5% | 0 | 0 | PASS |
| 5 MHz | `…1ef9c6`（RX 2420） | 0.05 | −0.02 | 0.04 | 0.16 | 0% → 0% | 0 | 0 | PASS |
| 4 MHz | `…1e6e77` | 0.14 | −0.28 | 0.31 | 0.20 | −1.5 個百分點 | 0 | 0 | PASS |
| 4 MHz | `…1ef9c6` | −0.07 | −0.07 | −0.11 | −0.10 | 0 → 0 | 0 | 0 | PASS |
| 8 MHz | `…1e6e77` | −0.04 | 0.03 | 0.03 | −0.01 | +2.5 個百分點 | 0 | 0 | FAIL（Δ遺失） |
| 8 MHz | `…1ef9c6` | 0.00 | 0.01 | 0.06 | −0.05 | 0 → 0 | 0 | 0 | PASS |

ΔN = 52 個 data carrier 的底噪，自己 burst 開與關的差；ΔSNR = peer 的 preamble SNR 中位數下降；
Δ遺失以 200 個 peer burst 計（1 個 = 0.5 個百分點）。

- 20／40／70 MHz、g = 15 與 30：ΔN 在 ±0.15 dB 內，ΔSNR ≤ 0.6 dB。g = 45 clip（1e-4 到 3.9e-2）。
- Phase a 的 Wi-Fi 佔用（g = 15／30／45）：2420 MHz 0／2.6／20%、2460 MHz 1.6／7.8／29%、2405 0／2.6／10%、
  2475 0／0／0.05%、2440 5.7–7.7%。
- 校準：g = 15 時 peer 在 −8 dB（A←B）／−14 dB（B←A）有 14–15 dB 的 SNR：**路徑不對稱約 6 dB**，B 聽 A
  比較清楚。
- S = 0（兩台都 2440 MHz、自己 −20 dB）：自己的 burst −35.4／−36.0 dBFS（最大 129／178 LSB，沒有 clip），
  底噪 −50.9／−53.1 dBFS；burst 結束後底噪（+3 dB 內）回來的時間中位數 46／14 samples、p90 845／224
  samples（0.17／0.05 ms）。Burst detector 把 OFDM 的包絡切成好幾段，所以這是上限。只給 LBT 參考，
  沒有用到。
- 每個組合的完整表：`results/si/run1/si_summary.md`；每個 phase 的紀錄（含每個 bin 的頻譜）：
  `si_scan.json`、`records.jsonl`。

### 4.3 M2：固定 MCS，雙向

**第一次 smoke 與 RX 修正之後的 smoke（實測）。** MCS 8、每方向 100 datagram/s × 900 B、20 秒、RX gain 15 dB。

| | smoke（`results/m2/smoke/`，`2496d7f`，修正前） | smoke2（`results/m2/smoke2/`，`16af5b9`，修正後；初步） |
|---|---|---|
| TX gain A／B | −20／−20 dB | −20／−14 dB |
| A→B 遺失（SNR） | 10.8%（9.9 dB） | **0%**（9.6 dB） |
| B→A 遺失（SNR） | 43.1%（3.0 dB，低於 MCS 8 的 7.5 dB） | 5.1%（8.4 dB，在 MCS 8 的邊緣） |
| RX overflow（前 3 秒以後）node 1／2 | 55／25 | **0／0** |
| 延遲 p50（A→B／B→A） | 284／299 ms | 61／63 ms |
| CPU，每個 node 所有 process 合計 | 133%／130% | 92%／94% |

A→B 兩次的 gain 與 SNR 相同，所以 10.8% → 0 與 overflow 25 → 0 是 RX 修正的效果。B→A 第二次把 node 2
的 gain 提高 6 dB（路徑不對稱），遺失的差主要是 SNR。

**Matrix（初步）。** WP-A 的 M2 matrix 在寫這份文件時還在跑（MCS 8 −20 dB 的 B→A 單向還沒完成）。下表是
已經完成的 run，在 run 目錄的副本上用 `summarize_duplex.py` 算出（`git 16af5b9`，WP-A 的 worktree 有未
commit 的修改）。900 B，每方向 MCS 12 450/s（3.24 Mbit/s）、MCS 8 180/s（1.30 Mbit/s），60 秒；node 2 的
TX gain 比 node 1 高（`--m2-gain2-offset`：−5 dB 那組 +5，其他 +6，路徑不對稱）。MCS 是固定的。
「遺失」是 udp_load 逐 byte 比對的 datagram 遺失，「subframe」是接收端的 PHY sequence gap。
Overflow 是接收端在前 3 秒以後的 RX overflow；CPU 是 node 所有 process 合計（parent 約 3%，其餘是 engine
子 process 與 3 個 decode workers），接收端／發送端。

A→B（node 1 → node 2，2420 MHz）：

| MCS | TX gain A／B | Run | Goodput | 遺失 | subframe | SNR | 延遲 p50／p99 | Overflow | CPU 收／發 |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 12 | −5／0 dB | 雙向 | 3.240 Mbit/s | 0 | 0 | 22.7 dB | 91／293 ms | 0 | 153%／154% |
| 12 | −5／0 dB | 單向 | 3.240 | 0 | 0 | 22.8 | 77／160 | 0 | 138%／94% |
| 12 | −12.5／−6.5 dB | 雙向 | 3.239 | 0.02% | 0.02% | 16.4 | 235／374 | 1 | 154%／158% |
| 12 | −12.5／−6.5 dB | 單向 | 3.240 | 0 | 0 | 16.4 | 79／143 | 0 | 139%／95% |
| 12 | −20／−14 dB | 雙向 | 2.439 | 24.7% | 24.7% | 9.6 | 546／670 | 1 | 225%／267% |
| 12 | −20／−14 dB | 單向 | 2.461 | 24.1% | 24.0% | 9.6 | 85／150 | 0 | 210%／90% |
| 8 | −20／−14 dB | 雙向 | 1.160 | 10.5% | 0 | 9.6 | 1766／1843 | 1 | 106%／106% |
| 8 | −20／−14 dB | 單向 | 1.220 | 5.9% | 0.02% | 9.6 | 1670／1751 | 0 | 94%／93% |

B→A（node 2 → node 1，2460 MHz）：

| MCS | TX gain A／B | Run | Goodput | 遺失 | subframe | SNR | 延遲 p50／p99 | Overflow | CPU 收／發 |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 12 | −5／0 dB | 雙向 | 3.212 Mbit/s | 0.86% | 0.86% | 21.0 dB | 284／382 ms | 3 | 154%／153% |
| 12 | −5／0 dB | 單向 | 3.214 | 0.81% | 0.81% | 21.2 | 107／231 | 0 | 139%／94% |
| 12 | −12.5／−6.5 dB | 雙向 | 3.132 | 3.33% | 2.96% | 15.7 | 585／698 | 1 | 158%／154% |
| 12 | −12.5／−6.5 dB | 單向 | 3.126 | 3.51% | 3.51% | 15.7 | 85／180 | 0 | 144%／94% |
| 12 | −20／−14 dB | 雙向 | 1.272 | 60.8% | 60.4% | 8.7 | 637／726 | 2 | 267%／225% |
| 12 | −20／−14 dB | 單向 | 1.296 | 60.0% | 60.0% | 8.8 | 96／156 | 0 | 251%／86% |
| 8 | −20／−14 dB | 雙向 | 1.079 | 16.8% | 5.0% | 8.4 | 1802／1886 | 0 | 106%／106% |
| 8 | −20／−14 dB | 單向 | 還在跑 | | | | | | |

- **雙向沒有比單向多掉。** 同一個方向、同一個 gain，雙向與單向的遺失差 ≤ 0.7 個百分點、SNR 差 ≤ 0.2 dB
  （MCS 8 的差在 TX 端，見下），與 M1「量不到 SI」一致。
- **SNR 與 threshold 表一致。** MCS 12 要 14 dB：22.7／21.0 dB 時 0–0.9%，16.4／15.7 dB 時 0–3.5%，
  9.6／8.7 dB 時 24–61%。−20 dB 正是 LA 應該降到 MCS 8–9 的地方（M3）。
- **B→A（2460 MHz）一直比 A→B 掉得多**，即使 SNR 比 threshold 高 7 dB（21.0 dB 時 0.8%）。遺失成串出現
  （−5 dB 雙向的第一批是 datagram 270–284 連續 15 個）；2460 MHz 在 Wi-Fi channel 11，M1 的 phase a 在這裡
  有 1.6% 的佔用（2420 MHz 是 0）。這是推測，沒有另外查。
- **雙向的延遲高 2–6 倍**：MCS 12 單向 p50 77–107 ms，雙向 91–637 ms，越接近邊緣越高。原因沒有查；設計稿
  的風險 3（同一個 engine 裡 RX owner 與 TX encode 搶 GIL）是候選，`split` layout 是為它準備的備案。
- **MCS 8 在 180/s 送不完。** A→B 的 subframe 遺失 ≈ 0，datagram 卻掉 5.9–10.5%、延遲 p50 1.7–1.8 s：
  遺失在 TX 端或 run 結束時還在 queue 裡，不在空中。Node 1 每個 burst 平均只帶 1.65 個（雙向）／1.98 個
  （單向）datagram，duty cap 沒有等過（`duty_cap_waits` 0），ingress 沒有因 queue 滿而丟。`tx_v5` 第一輪
  的 `tx_app` 在 MCS 8 有 1.748 Mbit/s，這裡只有 1.16–1.22。可能的原因（沒有查）：CONTROL subframe 佔掉
  MCS 8 的 8 ms airtime 上限裡的空間（2 個 900 B 已經 7.70 ms，雙向時 FEEDBACK 更大）；4 ms 最小 burst
  間隔從前一個 burst 推完才開始算（`tx_v5` 第一輪之後才加，MCS 8 沒有重量）。設計稿 `split` 的另一個觸發
  條件是「TX burst 率 < 同 MCS 的 `tx_app` 的 85%」。
- **低 SNR 時 decode 的 CPU 變高**：MCS 12 在 −20 dB 接收端 2.1–2.7 core（解不出的 burst 跑滿 Turbo 的
  iteration），高 SNR 時約 1.4–1.6 core。每個 run 的 RX overflow ≤ 3 次（≤ 3 次／分鐘）；設計的 `split`
  觸發條件是 > 1 次／分鐘。

【待填：M2】（WP-A 的 `results/m2/README.md` 完成後以它為準：補 MCS 8 −20 dB 的 B→A 單向、必要時的
−5 → −15 → −5 dB 變化，核對上面的初步數字與推測）

### 4.4 M3：LA 跟著 gain 變化

【待填：M3】

要填的表（每個方向；A→B 是 gain step 的方向）：

| Run | 0–30 s（−5 dB）goodput／遺失／MCS 中位數 | 30–60 s（−20 dB） | 60–90 s（−5 dB） | 整個 run goodput／遺失 | step 之後的收斂時間（30 s／60 s） | step 後第一秒遺失的 subframe |
|---|---|---|---|---|---|---|
| `olla` #1 | | | | | | |
| `olla` #2 | | | | | | |
| `olla`，B→A 閒置 | | | | | | |
| 固定 MCS 8 | | | | | — | |
| 固定 MCS 10 | | | | | — | |
| 固定 MCS 12 | | | | | — | |
| 固定 MCS 15 | | | | | — | |

另外：MCS 軌跡 vs. gain step 的圖（`duplex_AB.png`）、SNR_f 與 offset 的軌跡、與最好的固定 MCS（整個
run 遺失 ≤ 1%，以及每個 segment）的比較、B→A 方向（feedback piggyback 的方向）的 goodput 與遺失。

### 4.5 M4：雙向影像

【待填：M4】

要填的表：

| 方向 | 來源 | bitrate | 交付／遺失的 datagram | corrupt 行 | error 行 | 解出的 frame | goodput 中位數 | MCS 軌跡 |
|---|---|---:|---|---:|---:|---:|---:|---|
| A→B | webcam | 750 kbit/s | | | | | | |
| B→A | testsrc2 | 750 kbit/s | | | | | | |

## 5. ARQ 提案（沒有實作）

Owner 只要求提案。下面是 LTE RLC AM（TS 36.322）式的設計，以及它與這個 link 已有的 A-MPDU、feedback
通道與 LA 怎麼接。

**範圍。** 由應用程式選擇：AM 走另一個 ingress port（`base+5`，node 1 = 52215）並由另一個 egress port
交付（`base+6`，node 1 = 52216）。原本的 ports 保持 UDP 語意。影像這類「晚到不如不到」的流量繼續用
UDP，檔案、訊息這類要完整的流量用 AM。

**Data PDU。** 仍然是一個 DATA frame（一個 A-MPDU subframe）。Datagram header 現有的 `flags`（目前沒用）
bit 0 = AM，後面接 RLC header：SN（u16）、poll bit P、segment info SI。**SN 是獨立的序號空間**：PHY 的
frame sequence 不變，重送的 PDU 拿新的 PHY sequence，所以 LA 量到的 channel 遺失不受重送影響。
PDU 不再切割（LTE 有 re-segmentation）：datagram 層已經把訊息切成 ≤ 一個 subframe 的大小，降 MCS 也
放得進 aggregate。

**Status PDU。** LCP 的 `pdu_type 3 STATUS`（已保留；v1 的接收端忽略不認得的 type，所以向後相容）：

```
ACK_SN u16                 下一個還沒收到的 SN；之前的除了 NACK 的都收到了
NACK_START u16             bitmap 的起點
bitmap_len u8, bitmap      從 NACK_START 起每個 SN 一個 bit，1 = 遺失（一個整個掉的 aggregate 是一串 1）
n_ranges u8, ranges        選用：(start u16, len u16)，給 bitmap 裝不下的長串遺失
```

LCP 的 `body_length` 是 u8，所以 STATUS 最多 255 bytes：bitmap 128 個 SN 只要 16 bytes。STATUS 和
FEEDBACK 一樣 **piggyback 在 CONTROL subframe**（`encode_datagrams` 已經接受多個 `control_payloads`，一個
burst 可以同時帶 FEEDBACK 與 STATUS）；沒有 data 時走 control-only burst，而且受 robust copy 的規則保護
（MCS ≤ 9）。

**Timer 與參數**（LTE 的對應；數字是起點，要量）：

| 參數 | 建議 | 理由 |
|---|---:|---|
| Poll | 送完 buffer 最後一個 PDU、每 64 個 PDU、或每 64 KB 時設 P | LTE 的 pollPDU／pollByte |
| t-PollRetransmit | 120 ms | 約兩倍 feedback loop（RX chunk 13 ms + decode + 20 ms 窗 + 反方向 burst ≈ 50–70 ms）；逾時就重送最後一個 PDU 並設 P |
| t-Reordering | 設計稿 60 ms；可以縮到約一個 RX chunk | PHY 依序交付（decode pool 不改順序），所以 SN 的 gap 幾乎一定是遺失，不是亂序；t-Reordering 只要蓋過 pool 的批次 |
| t-StatusProhibit | 20 ms | 等於 reporter 的 period，兩份 STATUS 之間至少 20 ms，不會淹沒反方向 |
| Window | 2048 個 SN | 9.3 Mbit/s × 約 300 ms（loop + 容量邊緣的排隊）≈ 350 KB ≈ 390 個 900 B PDU，2048 有餘裕；u16 SN 時上限 32 768 |
| maxRetx | 4 | 之後丟掉這個 SDU、計數，AM egress 跳過它繼續交付 |

**TX 端。** Retransmission buffer 保留還沒 ACK 的 PDU（2048 × 約 900 B ≈ 1.8 MB）。NACK 的 PDU 排在新
data **之前**，MCS 不高於 LA 的決定（可以低一階，讓重送更可能成功）。重送的 PDU 照樣算在 aggregation
的 subframe、byte、airtime 上限內。

**RX 端。** Reorder buffer 依 SN 依序交付到 AM egress；有 gap 時 t-Reordering 到期就送 STATUS。

**與 A-MPDU 的關係。** 一個 aggregate 掉了就是一串連續的 SN，所以 bitmap（或一個 range）很省。FCS 失敗
但 subframe CRC 正確的仍然交付（現有行為），只有真的壞掉的 subframe 進 NACK。

**與 LA 的關係。** LA 的 outer loop 仍用 PHY 的 DATA subframe 遺失（feedback 的累計計數器），不看 ARQ
的結果，所以重送不會被當成 channel 變好或變差。有 ARQ 之後，1% 的 target 可以放寬（LTE 在有 HARQ 時用
10%）：更高的 MCS、更多重送，換更高的 goodput 與更長的延遲。這要另外量，AM 與 UDP 流量共用 link 時
target 也要妥協。

**代價。**

- **延遲與 head-of-line blocking**：一個遺失讓後面的 PDU 至少等一個 feedback loop（50–120 ms），重送
  再掉就再等一次。影像不適合，所以是 opt-in。
- **反方向的依賴**：STATUS 掉了要靠 poll 與 t-PollRetransmit；反方向斷線時 TX 的 window 會停住。
- **Overhead**：RLC header 約 4 bytes／PDU（900 B 的 0.4%），STATUS 每 20 ms 最多 255 bytes；重送本身
  吃掉約等於遺失率的容量。
- **記憶體與 CPU**：retransmission 與 reorder buffer 各約 1.8 MB；Python 的 timer 與 bitmap 處理在
  engine 子 process 裡，和 RX owner 搶 GIL（M2 的 CPU 已經是每個 node 約 1.5 core）。
- **複雜度與測試**：要用假的 link（隨機、成串、整個 aggregate 的遺失）做單元測試，UDP transport 的雙向
  測試，再上 M3 那樣的 gain step 量殘餘遺失與延遲分布。

**HARQ（soft combining）不在範圍內。** 它需要嚴格的時序（固定的 ACK 時間、保留 LLR 的 buffer），
Pluto 沒有 timestamp，給不了。

## 6. 檔案

| 檔案 | 內容 |
|---|---|
| `usb_capacity.py` | M0：USB 頻寬、第二個 context 的 probe |
| `si_scan.py` | M1：自干擾的 phases、離線解碼、判定規則（`run`／`summarize`／`--dry-run`） |
| `hw_common.py` | 硬體腳本共用：hardware lock、load gate、meta、recorder |
| `run_duplex.sh` | 一次雙向 run：兩個 headless `node_app`、每個方向一個 `udp_load.py` |
| `run_matrix.sh` | M2（`fixed`）與 M3（`la`）的 matrix |
| `video_duplex.sh`、`video_counts.py` | M4：雙向影像與 corrupt／error 的計數 |
| `summarize_duplex.py` | 每個 run 的 `summary.json`、`summary.md`、PNG |
| `cpu_multi.py`、`write_meta.py` | 每個 node 的 CPU 取樣；run 的 `meta.json` |
| `results/usb/`、`results/usb_duty/` | M0（`usb_capacity.json`、`usb_summary.md`、`records.jsonl`、`meta.json`） |
| `results/si/README.md`、`results/si/run1/` | M0／M1 的整理與決定；`si_summary.md`、`si_scan.json`、`records.jsonl` |
| `results/m2/` | M2：`README.md`、`smoke/`、`smoke2/` 與 matrix 的 run 目錄（每個有 stats、LA log、load、CPU、meta） |
| `results/m3/`、`results/m4/` | M3、M4（量測中） |

程式（`src/ofdm_message_link/`）：`node_app.py`、`node_options.py`、`node_engine.py`、`node_window.py`、
`control.py`、`la/`（`api`、`loader`、`thresholds`、`fixed`、`olla`、`reporter`、`examples`、`controller`）、
`pluto.py`（`SharedPlutoContext`）、`link.py`／`tx_app.py`／`rx_app.py`（hooks）；設定
`configs/profiles/duplex_fdd_2420_2460.yaml`、`duplex_fdd_2405_2475.yaml`、`configs/la/thresholds_pluto_v5.yaml`。

測試（`tests/app/`）：LA `test_la_loader.py`、`test_la_olla.py`（假的 logistic loss
link：12／16／21 dB 收斂、threshold 偏 ±3 dB、hysteresis、stale、u32 wrap、overflow、平衡點）、
`test_la_reporter.py`、`test_la_examples.py`、`test_la_controller.py`、`test_thresholds.py`；wire 與 hooks
`test_control_wire.py`、`test_link_control.py`、`test_tx_policy.py`（含 golden hash）、`test_rx_observer.py`；
node `test_pluto_shared.py`、`test_node_options.py`、`test_config_duplex.py`、`test_node_engine.py`、
`test_node_headless.py`、`test_node_udp_duplex.py`（兩個 engine 經 UDP，兩個方向逐 byte 正確、feedback
兩個方向都有）、`test_node_window.py`；量測腳本 `test_duplex_experiments.py`、`test_summarize_duplex.py`。

## 7. 證據邊界

- 只有 Pluto 對 Pluto、一個擺放位置、一天、一組頻率。B210、N210、USRP-2901 沒有跑 `node_app`
  （`SharedPlutoContext` 只有 Pluto 有）。
- M1 的遺失每個 phase 只有 200 個 burst（1 個 = 0.5 個百分點），只能說「量不到 SI」，不能排除小於
  約 0.5 dB 的影響；自己的 duty 是 0.7，不是設計的 0.9。
- `…1e6e77` 在 duty 0.78 會 overflow、`…1ef9c6` 不會，原因（裝置或 port）沒有分開查；0.7 的上限是
  由這兩台、這台 PC 的 USB 拓撲量出來的。
- 2420／2460 在 Wi-Fi channel 1／11 裡，遺失含有 Wi-Fi 的部分，沒有分開。
- M2 的 matrix 在寫這份文件時還沒跑完；4.3 的 matrix 數字是初步的，WP-A 的 worktree 有未 commit 的
  修改（`meta.json` 的 `git_dirty`）。M3、M4 在寫這份文件時還沒有數據。
- 每點只跑一次；延遲包含 TX 排隊，收發在同一台 PC。
- 2.5 的 bug 修正數字來自 review script 與單元測試的假 link，不是硬體。
- LBT 與 `split` layout 只有設計，沒有實作；ARQ 只有提案。
- `node_app` 的 `--link-adaptation` 預設是 `fixed`，不是 owner 要求的 `olla`（1 節第 7f 項）。

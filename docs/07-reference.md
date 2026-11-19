# 7. 參考資料

## 7.1 命令列參數

`python -m ofdm_message_link.tx_app --help` 與 `rx_app --help` 會列出完整說明。

### 兩支程式共用

| 選項 | 說明 |
|---|---|
| `--config` / `--overlay` | 設定檔，預設 `configs/default.yaml` + `configs/profiles/ota_2p45ghz.yaml`；`--overlay` 可重複 |
| `--transport {udp,uhd,pluto}` | 預設 `udp`（模擬）；`uhd` 是 USRP，`pluto` 是 ADALM-Pluto |
| `--mcs {qpsk,qam16}` | TX 的簡寫：`qpsk` = MCS 8（QPSK 1/3）、`qam16` = MCS 12（16QAM 1/2）；同時給 `--mcs-index` 時後者優先。RX 僅作 display hint，不參與 decode |
| `--mcs-index N` | TX 啟動時的 wire-v5 MCS，8–17，**預設 8**（給 `--mcs qam16` 時是 12）。11 是選單隱藏的實驗用 MCS。舊格式的 0–7 TX 會拒絕；RX 要設 `phy.rx.legacy_acquisition: true`（v2.0 起預設關）才解舊版 TX 送的 0–7 burst。RX 接受 0–17，僅作 display hint |
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
| `--cpu-affinity CPUS` | 把 app 與它啟動的每個 process（engine、decode workers）綁在 CPUS：`0-7`、`2,4-5` 這種列表，或 `p-cores`（讀 `/sys/devices/cpu_core/cpus`，沒有這個檔就不綁，記一行 log）。**預設不綁**。見 [7.10 節](#710-效能開關與-native-模組) |
| `--realtime[=PRIO]` | 只限 Linux：engine process 的 thread 用 SCHED_RR、優先權 PRIO（1–49，只寫 `--realtime` 時是 15），decode workers 維持一般排程，不動 RT throttling。可用 CPU 少於 4 個時拒絕，除非加 `--force`。**預設關閉** |
| `--force` | 允許在少於 4 個可用 CPU 的機器上用 `--realtime` |
| `--native-backend {auto,python,native}` | Acquisition、header probe、Turbo kernel、TX encode 用 native 還是 Python 參考實作；預設 `auto`（有 build 就用 native，沒有就退回 Python 並警告一次）。設定 `OFDM_LINK_NATIVE` |
| `--cpu-cores N` | App 的主 process 與 engine 子 process 各自可用的 NumPy／BLAS（與 OpenMP）thread 數，0–256；0 = 這個 process 可以跑的 CPU 數（看 affinity）。沒給時取 `acceleration.cpu_cores`，設定檔也沒有時是 1。Decode workers 固定各 1 個 thread。見 [7.10 節](#710-效能開關與-native-模組) |

### 只有發送端

| 選項 | 說明 |
|---|---|
| `--stats-log PATH` | 每次統計刷新附加一行 JSON：messages、bursts、goodput、airtime 與 `tx_health`（見下）。`t` 是 monotonic 時間，`wall_time` 是 Unix 時間（秒），用來和其他機器的紀錄對時。`goodput_bps`、`airtime` 是 2 秒視窗，流量停了就歸零；`goodput_avg_bps`、`airtime_avg` 是開始（或重設統計）以來的平均。`--stats-log-max-mb`／`--stats-log-backups` 與接收端相同 |
| `--ingress-port` | 接受 application bytes 的 UDP port，預設 52001 |
| `--peak-amplitude` | 每個 burst 發射前正規化到的 peak，預設 0.7 |
| `--aggregation {on,off}` | `on`：radio 可以接下一個 burst 時，把 queue 裡的 datagram 放進同一個 burst（在下面的上限內）。`off`：一個 burst 一個 datagram，供 A/B 量測。預設取 `mac.aggregation.enabled`（`on`） |
| `--aggregation-max-subframes N` | 一個 burst 最多幾個 datagram，1–64，預設取設定檔（32） |
| `--aggregation-max-wait-ms MS` | 還有空間的 burst 最多等 MS 毫秒（從最早的 message 到達算起），讓更多 datagram 一起走。0–1000，預設 0：不等，負載低時不增加延遲 |
| `--min-burst-interval-ms MS` | 兩個 burst 開始交給 radio 的時間至少隔多久（start 到 start：比間隔長的 burst 後面不會多等），0–50，預設取設定檔（4 ms，最多每秒 250 個 burst）；等的時候繼續收 datagram，讓下一個 burst 帶比較多。閒置之後的第一個 burst 不多等，滿的 burst 不會被留。0 關閉。實際等待是它與 `--aggregation-max-wait-ms` 較大的那個。PC 送往 Jetson 時用 25（[`tx_to_jetson.yaml`](../configs/profiles/tx_to_jetson.yaml)，7.10 節） |
| `--max-burst-airtime-ms MS` | 聚合後的 burst 最長多久（含 guard），大於 0、最大 100，預設取設定檔（8 ms）。不論設多少，聚合也會在 80 000 samples 停止；超過上限的單一 datagram 仍然單獨送出 |
| `--enable-rf` / `--acknowledgement` | RF 發射授權，`--transport uhd` 與 `--transport pluto` 必要。Acknowledgement 的文字是 `I acknowledge that this process will transmit RF`，必須一字不差 |

### 只有接收端

| 選項 | 說明 |
|---|---|
| `--egress-host` / `--egress-port` | 轉發已交付訊息的目的地，預設 `127.0.0.1:52002` |
| `--snr-db` | 只適用 `udp`，加上 AWGN。對真實 channel 無效，帶了會被拒絕 |
| `--decode-workers N` | Payload decode 的 process 數，1–64；1 = 單 thread inline decoder。沒給時取 `acceleration.decode_workers`，設定檔是 `null`（預設）時是 4 |
| `--rx-recv-frames` | UHD `num_recv_frames`，預設每 5 MS/s 256 個（約 0.1 s）；0 = UHD 預設 |
| `--rx-min-feed-samples N` / `--rx-max-merge-wait-ms MS` | 把 transport 的短 chunk 併成至少 N samples（0–131072）再交給 decoder，最多為此等 MS 毫秒（0–20）。沒給時取 `acceleration.rx_min_feed_samples`／`rx_max_merge_wait_ms`，設定檔是 `null`（預設）時是 8192 與 2 ms。Jetson 用 65536 與 15（[`jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml)）。見 [7.10 節](#710-效能開關與-native-模組) |
| `--stats-log PATH` | 每次統計刷新附加一行 JSON，見 [2.7 節](02-gui.md#27---stats-log把統計存成檔案) |
| `--stats-log-max-mb MB` | `--stats-log` 的大小上限，預設 16 MiB，超過就輪替成 `PATH.1`；0 = 不設限 |
| `--stats-log-backups N` | 保留幾份輪替後的舊檔，預設 1 |
| `--overload-protection {off,drop,give-up,both}` | 過載保護，**預設 `off`，量測一律關閉**。`drop`：decode pool 滿時丟掉放不下的 burst，不讓 receiver 等；`give-up`：Turbo block 到第 4 次 iteration 還有超過 1/64 的 bit 在翻，就放棄。兩者各有自己的計數器，見 [7.10 節](#710-效能開關與-native-模組) |

**TX health（`tx_health`，`tx_app --stats-log` 與 `node_app` 的 `tx.health`）。** 一個平的紀錄，發送端每個會掉東西的地方各有自己的計數器
（累計值；深度是當下的值）。Transport 沒有的計數器寫 `null`，不寫 0：

| 欄位 | 意義 |
|---|---|
| `queue_datagrams` | 發送端收下、還沒完全送上空中的 datagram 數 |
| `queue_bursts` | Radio sink 裡等著送的 burst 數（UHD） |
| `ingress_dropped_queue_full` | UDP ingress 收到、但發送 queue 滿了被拒絕的 datagram（以前 `tx_app` 默默丟掉） |
| `bursts_rejected_queue_full` | Radio sink 的 burst queue 滿了而拒絕的 burst |
| `tx_underflow`、`tx_underflow_in_packet` | UHD 在 burst 之前／之中沒有 sample 可送 |
| `tx_late` | UHD 的 `tx_time_error`：burst 到 radio 時已經過了它的開始時間 |
| `dropped_encode_failed` | 因為 encode 丟出例外而丟掉的 datagram（程式錯誤；同樣的輸入重試也會一樣失敗，所以不重試） |
| `send_failures` | Transport 的 send 丟出例外的次數（之後保留重送或丟掉都算）。udp 與 UHD 的 sink（`retry_failed_send`：丟出例外時沒有送出接收端會收下的東西）會保留那個 burst（原本的 sequence number 與 MCS），每 50 ms 重送，成功後才送下一個；同一個 burst 在同一個 sink 上失敗超過 2 s 就丟掉，之後每個 burst 只試一次，直到有一次成功。Pluto 照舊直接丟掉（push 逾時時可能已經送出，重送會重複）。udp 重送前送出的部分 fragment 會在接收端算成一次 `bursts_incomplete` |
| `dropped_send_failed` | 因為 send 丟出例外而丟掉的 datagram（Pluto 立刻丟；udp／UHD 重送超過上限才丟） |

## 7.2 設定檔

設定分兩層：[`configs/default.yaml`](../configs/default.yaml) 是基底，再疊上一個 profile
（`--overlay`，可重複，後面的蓋過前面的）。

| Profile | 內容 |
|---|---|
| [`ota_2p45ghz.yaml`](../configs/profiles/ota_2p45ghz.yaml) | **預設**。2.45 GHz、5 MS/s |
| [`usrp_ota_1p2ghz.yaml`](../configs/profiles/usrp_ota_1p2ghz.yaml) | 選用。1.2 GHz、5 MS/s。這個頻段配置給 GNSS，只在確定可以使用時才用 |
| [`b210_ota_3p8ghz.yaml`](../configs/profiles/b210_ota_3p8ghz.yaml) | 選用。3.8 GHz、5 MS/s，gain 設定很高 |
| [`b210_ota_3p8ghz_10msps.yaml`](../configs/profiles/b210_ota_3p8ghz_10msps.yaml) | 選用。同上但 10 MS/s |
| [`duplex_fdd_2420_2460.yaml`](../configs/profiles/duplex_fdd_2420_2460.yaml) | `node_app` 的預設。兩台 Pluto、5 MS/s；node 1 TX 2420／RX 2460 MHz，node 2 相反 |
| [`duplex_fdd_2405_2475.yaml`](../configs/profiles/duplex_fdd_2405_2475.yaml) | `node_app` 的備用：70 MHz 間距，2405／2475 MHz |
| [`duplex_fdd_2420_2460_15p36.yaml`](../configs/profiles/duplex_fdd_2420_2460_15p36.yaml) | `node_app`，兩台 USRP（`--transport uhd`）、15.36 MS/s，同 2420／2460 MHz（7.4 節）；雙向還沒有量測紀錄 |
| [`b210_2p45ghz_15p36msps.yaml`](../configs/profiles/b210_2p45ghz_15p36msps.yaml) | `tx_app`／`rx_app`，USRP（`--transport uhd`）、15.36 MS/s、2.45 GHz（7.4 節） |
| [`legacy_rx.yaml`](../configs/profiles/legacy_rx.yaml) | **附加用**：把 `phy.rx.legacy_acquisition` 打開，也收 v1.0／`improve-rx` 的 TX 送的舊格式 burst（v2.0 起預設不收） |
| [`b210_measurement.yaml`](../configs/profiles/b210_measurement.yaml) | **附加用**，接在上面任一個 profile 後面：把 `phy.rx.legacy_acquisition` 關掉。v2.0 起預設本來就是關，它只在底下的設定把它打開時才有作用（`run_duplex.sh --transport uhd` 仍會自動加） |
| [`jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml) | **附加用**，接在 profile 後面，只用在 Jetson 的接收端（`rx_app`／`node_app`）：`acceleration.rx_min_feed_samples` 65536、`rx_max_merge_wait_ms` 15。量測與適用範圍見 7.10 節 |
| [`tx_to_jetson.yaml`](../configs/profiles/tx_to_jetson.yaml) | **附加用**，接在 profile 後面，只用在送往 Jetson 的 PC 發送端（`tx_app`，或雙向時 PC 的 `node_app`）：`mac.aggregation.min_burst_interval_ms` 25。量測與適用範圍見 7.10 節 |

這個專案實際讀取的區段：

| 區段 | 用途 |
|---|---|
| `phy` | Sample rate、center frequency、預設 MCS 與 FEC、synchronization 的門檻；`phy.v5`、`phy.rx` 見下 |
| `mac.aggregation` | v5 TX 的 aggregation 上限，見下 |
| `radio` | `adapter`（`uhd` 才能用 `--transport uhd`）、預設 gain 與 antenna、clock source |
| `acceleration` | `decode_workers`、`cpu_cores`、`rx_min_feed_samples`、`rx_max_merge_wait_ms`：decode worker 數、NumPy 的 thread 數與接收端 feed 的大小，見下 |
| `duplex` | 只有 `node_app` 讀（選用）：`mode`、`control_mcs_index`、`nodes`，見下 |

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
| `legacy_acquisition` | `false`（v2.0 起） | `true`：也收 v1–v4 的舊格式 burst（v1.0 的 TX 只送舊格式）。這個 repo 的 TX 只送 v5，開著只會讓 receive owner 多花 10–16%（有 DC 時 44–46%） |
| `v5_acquisition` | `true` | 實驗。關掉 v5 acquisition |
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
| `snr_fft_advance_samples` | 0 | 量測選項（v2.1.1 起）：preamble SNR 估計的兩個 LTF FFT window 提早幾個 sample（0–8）。只改 SNR 的讀值（`link_snr`、`--events` 的 `snr_db`、給 link adaptation 的 feedback），不改解碼。0 是 v2.1.0 的估計；`node_app --snr-fft-advance` 可以覆寫 |

`duplex`（只有 `node_app` 讀；`tx_app` 與 `rx_app` 忽略它）：

| Key | 預設 | 說明 |
|---|---|---|
| `mode` | 必填 | `fdd`：每個 node 有自己的 TX 頻率，兩個頻率至少相隔一個 sample rate。`lbt`（同一個頻率）只有設定 schema 與 `duplex.lbt` 的參數，`node_app` 沒有實作 listen-before-talk（M1 量到不需要） |
| `control_mcs_index` | 8 | Control-only burst 的 MCS（8–17）；`--control-mcs-index` 會蓋過它 |
| `nodes` | 必填 | 剛好兩個 `{node_id, tx_frequency, rx_frequency}`；id 不能相同，一個 node 的 `tx_frequency` 必須是另一個的 `rx_frequency`。Schema 接受 id 1–254，`node_app --node-id` 只接受 1 與 2 |

`acceleration`（三支 app 都讀；命令列的同名選項會蓋過它）：

| Key | 預設 | 範圍 | 說明 |
|---|---|---|---|
| `decode_workers` | `null` | `null`、0 或 1–64 | `rx_app` 與 `node_app` 的 payload decode process 數。`null`（或不寫）與 0 = 各 app 自己的預設（v2.0.0–v2.1.2 的 `default.yaml` 把 0 寫成「auto」）：`rx_app` 4、`node_app` 3（兩個 node 常在同一台 PC）。1 = 不開 worker，inline decode。`--decode-workers` 優先。`tx_app` 不 decode，不讀它 |
| `cpu_cores` | 1 | 0–256 | 每個 app 的主 process 與 engine 子 process 各自的 NumPy／BLAS（與 OpenMP）thread 上限；0 = 這個 process 可以跑的 CPU 數（看 affinity／cgroup）。`--cpu-cores` 優先。Decode workers 固定各 1 個 thread，平行度由 `decode_workers` 決定 |
| `rx_min_feed_samples` | `null` | `null` 或 0–131072（整數） | `rx_app` 與 `node_app`：比這個短的 transport chunk 會和 queue 裡後面的 chunk 併成一次 feed。`null` = 8192；0 = 每個 chunk 直接 feed。Jetson 用 65536（[`jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml)）。`--rx-min-feed-samples` 優先 |
| `rx_max_merge_wait_ms` | `null` | `null` 或 0–20（可以有小數） | 為了湊滿上面的大小最多等幾毫秒。`null` = 2；Jetson 用 15（同一個檔）。`--rx-max-merge-wait-ms` 優先 |

優先順序都是：命令列 > 設定檔（`default.yaml` 加上各個 overlay，後面的蓋過前面的）> app 內建預設。內建預設就是這些 key
生效之前的行為（thread 1、`rx_app` 4 個 worker、`node_app` 3 個、feed 8192 samples／2 ms），所以沒設的人執行結果不變。
輸出不會因這些值而改變，只影響 CPU 用量（feed 的兩個值另外會讓 `--events` 的主機時間晚一點，見 7.11 節）。超出範圍、
型別不對（包含 `true`、`"4"`；整數的 key 給 2.5）會在啟動時被拒絕。每個 app 啟動時印一行，寫出實際用的值與
來源，例如 `[acceleration] cpu_cores 1 from app default; decode_workers 4 from app default`；`rx_app` 與 `node_app`
後面再接 `; rx_min_feed_samples 8192 from app default; rx_max_merge_wait_ms 2 from app default`。

**從 v2.0–v2.1.2 複製來的設定檔要注意（v2.1.2 之後）：** 那幾版的 `default.yaml` 寫 `decode_workers: 1`、`cpu_cores: 4`，
但程式從來不讀。現在這兩個 key 會生效，所以這樣的設定檔會真的用 1 個 decode worker（inline decode）與 4 個 NumPy thread。
要維持以前的行為，把兩行刪掉或改成 `null`。例如在 overlay 裡寫：

```yaml
acceleration:
  decode_workers: 6
  cpu_cores: 2
```

`mac.aggregation`（只影響 TX；命令列的 aggregation 選項會蓋過它）：

| Key | 預設 | 說明 |
|---|---|---|
| `enabled` | `true` | `false`：一個 burst 一個 datagram |
| `max_wait_ms` | 0.0 | 還有空間的 burst 最多等多久 |
| `min_burst_interval_ms` | 4.0 | 兩個 burst 的開始時間至少隔多久（start 到 start，0–50，0 關閉）。短 datagram 時避免每個 datagram 一個 burst：Pluto 推送一次要 0.413 ms + 0.194 µs/sample，RX 每個 burst 也有成本。`AggregationLimits` 本身的預設是 0，直接呼叫的程式碼行為不變。PC 送往 Jetson 時用 25（[`tx_to_jetson.yaml`](../configs/profiles/tx_to_jetson.yaml)） |
| `max_subframes` | 32（v2.0 起；之前 16） | 一個 burst 最多幾個 datagram |
| `max_psdu_bytes` | 16384（v2.0 起；之前 8192） | 一個 burst 的 PSDU 最多幾個 byte；不能超過 `phy.rx.max_psdu_bytes`（16384），否則設定檔會被拒絕。B210 的最大吞吐測試（2026-11-11）在 10 MS/s MCS 17 用 16384／32 撐住 24.0 Mb/s，8192 時是 20.7 Mb/s |
| `max_burst_airtime_ms` | 8.0 | 聚合後的 burst 最長多久。換算成 sample 不能超過 80 000（5 MS/s 是 16 ms、10 MS/s 是 8 ms、15.36 MS/s 是 5.21 ms、20 MS/s 是 4 ms），否則設定檔會被拒絕 |

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

舊格式（wire v1–v4）的 MCS，**只用在 RX 解 v1.0.0 或 `improve-rx` 的 TX 送的 burst**（需要 `phy.rx.legacy_acquisition: true`，v2.0 起預設關）；現在的 TX 不送：

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

這條規則適用 `tx_app` 的 TX 與 `rx_app` 的 RX（`options.radio_settings`），不能調：overlay 的 `radio.bandwidth` 對它們
**沒有作用**，B210 讀回的值仍等於 sample rate。這樣切換 sample rate 時 filter 一定跟著變。`node_app` 的 TX 因為 LO 偏離載波，
預設較寬，可以用 `--tx-analog-bw` 調（見 7.9 節）。2026-11-12 評估過讓 `tx_app` 也可調：沒有量測顯示需要（`node_app` 用
5 MHz 的 TX filter 也正常），設太寬只會增加帶外發射，所以不做。

這個 PHY 使用 64-point FFT，佔用的 subcarrier 是 −26…−1 與 +1…+26（48 data + 4 pilots，DC 為空），
所以 occupied bandwidth 是 `52/64 × Fs = 0.8125 Fs`：

| Sample rate | OFDM occupied bandwidth | Analog bandwidth |
|---:|---:|---:|
| 5 MS/s | 4.06 MHz | 5 MHz |
| 10 MS/s | 8.13 MHz | 10 MHz |
| 15.36 MS/s（`uhd`；測過 B210 與 NI USRP-2901） | 12.48 MHz | 15.36 MHz |
| 20 MS/s | 16.25 MHz | 20 MHz |

**15.36 MS/s（`--transport uhd`；測過 B210 與 NI USRP-2901）。** LTE 的 sample rate。它需要 30.72 MHz 的 master clock：程式要求
UHD 把 master clock 設成 30.72 MHz（`ofdm_link.radio.uhd.MASTER_CLOCK_RATES`），B2xx 家族（B210、NI USRP-2901）的 FPGA 剛好
2 倍 decimation；30.72 MHz 也是 B210 兩個 channel 同時用時允許的最高值（`node_app` 一台裝置 TX 與 RX 各用一個前端）。其他速率
仍由 UHD 自己選 master clock。Master clock 固定的裝置（例如 N210 的 100 MHz）做不出剛好 15.36 MS/s。程式不依裝置型號限制，
但會比對 UHD 讀回的 sample rate：和要求的不同時記一行警告（不會停下來），這時兩端對不上，收不到東西。Pluto 經 USB 2.0
只能跑 5 MS/s。用 profile
[`duplex_fdd_2420_2460_15p36.yaml`](../configs/profiles/duplex_fdd_2420_2460_15p36.yaml)（`node_app`）或
[`b210_2p45ghz_15p36msps.yaml`](../configs/profiles/b210_2p45ghz_15p36msps.yaml)（`tx_app`／`rx_app`）。這個速率下：
aggregate 的 airtime 上限要 ≤ 5.21 ms（80 000 samples），profile 設 5 ms，沿用 8 ms 預設值的設定檔會被拒絕；RX 的 UHD
receive frames 與 chunk 依 sample rate 放大到 1024 個 frame 與 4 × 65536 samples；`node_app` 的 TX analog filter 預設是
35 MHz（見 7.9 節的 `--tx-analog-bw`）。

實測（[8.10](08-measurements.md#810-1536-mss-單向ni-usrp-2901--b21024402460-mhz)）：2026-11-11 在同一張桌上的 NI USRP-2901 與
B210 之間只量過單向（`tx_app` → `rx_app`）。MCS 17 約 35.5 Mb/s，上限是 TX airtime（duty 約 97.5 %）。兩個方向的 300 秒確認遺失
0.19 %／0.40 %，但**都判 FAIL**：TX 的 time_error 與 underflow（或被拒絕的 burst）在最後三分之一仍在增加。2901 → B210 方向有時
一啟動就壞：60 秒的 4 次啟動掉 13–28 %，15 秒的點有 31 % 到 99 %。雙向的 `node_app` 在 15.36 MS/s 沒有量過（兩台裝置 2x2 串流
15.36 MS/s 沒有 overflow，見 [`experiments/duplex_fdd/`](../experiments/duplex_fdd/README.md) 4.7 節，那只是 USB 的測試）。
12.5 MS/s 沒有開放。

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
- 執行中換 MCS 不遺失、不亂序、不用錯 MCS（`test_mcs_switch_guard.py`、`test_mcs_switch_node_udp.py`）；兩個
  `node_app --events` 經 udp 交換時，發送端每個 burst 一行 `sent`，sequence 連續，和 peer 解出的 `burst` 一個對一個、
  MCS 相同；在 aggregation 等待中換了 MCS 的 burst，`planned_mcs` 是換之前的 MCS；`--la-log` 的 `la` 行在同一個
  `first_sequence` 以最後一行為準（7.9 節的 `--la-log`），那一行的 MCS 就是從那裡開始的 data burst 的 MCS
  （`test_sent_events.py`）。

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

## 7.8 雙向 link 的 control 與 feedback wire format

只有雙向的 `node_app` 會送 control frame。`tx_app` 與 `rx_app` 沒有設定 hook 時，送出與接收的內容
都和以前一樣（`tests/app/test_tx_policy.py` 用固定 seed 比對 burst 的 hash）。

**位置。** Node 的每個 v5 burst 的第一個 MPDU 是一個 `FrameKind.CONTROL` frame：`mcs` = 該 burst
的 modulation，`sequence` 來自一個**獨立的** 16-bit control counter，payload 是一個 Link Control PDU
（LCP）。Reporter 有 report 到期時 LCP 是 FEEDBACK，否則是只有 header 的 LINK_HEADER。DATA frame 的
sequence 因此保持連續，舊版 `rx_app` 不會看到假的 gap，只把 CONTROL frame 記在 `control_frames`。
沒有 data 而 report 到期時，node 送一個只含 CONTROL frame 的 control-only burst（`--control-mcs-index`，
預設 MCS 8）；沒有 data 也沒有 report 時什麼都不送。兩個 node 用不同的 scrambler seed 範圍：node 1 用
1–63，node 2 用 64–127（`tx_app` 仍用 1–127）；RX 由 SIG 的 seed 在 payload decode 之前丟掉自己 TX 的洩漏。

**LCP header（8 bytes，big-endian）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | magic | u16 | `0x4C43`（"LC"） |
| 2 | version | u8 | 1；其他版本整個 LCP 被忽略 |
| 3 | pdu_type | u8 | 1 LINK_HEADER、2 FEEDBACK、3 STATUS（保留給 ARQ）；不認得的 type 被忽略 |
| 4 | src_node | u8 | 1–254；等於自己的 id 時，整個 PSDU 是自己 TX 的洩漏，丟棄（seed 範圍之外的第二道檢查） |
| 5 | dst_node | u8 | 0 = 任何 node；node 送 peer 的 id。不是 0 也不是自己時，內容被忽略（`control_other_dst`） |
| 6 | flags | u8 | b0 CONTROL_ONLY（control-only burst）、b1 LBT_MODE（保留，沒有使用）、b2 TX_QUEUE_NONEMPTY（有 data 的 burst） |
| 7 | body_length | u8 | LINK_HEADER 為 0 |

**FEEDBACK body v1（36 bytes 固定部分）**

| Offset | 欄位 | 型別 | 說明 |
|---:|---|---|---|
| 0 | report_seq | u16 | 每份 report +1（mod 2^16）；TX 的 `FeedbackTracker` 忽略晚到或重複的 report，peer 重新啟動時重設基準 |
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
| 36 | entries | 6 B 各一 | seq_first u16、mcs u8（`0xFF` = 無）、snr u8（0.25 dB 一階，0–63.5 dB 飽和；`0xFF` = 無）、ok u8、lost u8（飽和在 255）；只留最近的 16 個 |
| … | TLVs | | type u8、len u8、value。0x10 SESSION_ID：len 4、u32，reporter 啟動時隨機抽，encode 時永遠放第一個；peer 的 session id 改變時 TX 立即重設基準。0x80 以下保留給這個 codec，0x80–0xFF 給自訂 reporter；不認得的 TLV 放進 `extras` |

- 計數器是累計值，所以掉了幾份 feedback 也不會遺失資訊：TX 把收到的兩份相減。
- 解碼端遇到任何格式錯誤都回傳 `None`，不會丟例外；body 比已知的長時，多出的部分被忽略。
- 成本（在 burst 裡，含 frame header、CRC、delimiter 與 padding）：LINK_HEADER 每個 burst 24 bytes；
  內建 reporter 的 FEEDBACK（含 6-byte 的 session id TLV）沒有 entry 時 68 bytes、4 個 entries 92 bytes、
  8 個（內建 reporter 的上限）116 bytes。4 個 entries 的 control-only burst 在 MCS 8 是 2 560 samples（0.51 ms）。
- Data 的 MCS 高於 9 時，每 100 ms 至少有一份 feedback 走 MCS ≤ 9 的 burst（在 data burst 之前送一個
  control-only burst），data 的 MCS 太高時 feedback 仍然到得了。
- `pdu_type 3 STATUS` 保留給 ARQ，見[提案](../experiments/duplex_fdd/README.md#5-arq-提案沒有實作)。
- Codec 在 [`ofdm_message_link/control.py`](../src/ofdm_message_link/control.py)，report 的資料結構在
  [`ofdm_message_link/la/api.py`](../src/ofdm_message_link/la/api.py)。

## 7.9 `node_app`：雙向 node 的命令列

架構圖見 [1.7 節](01-architecture.md#17-雙向-nodenode_app)。一個 Pluto（或一台 USRP B210）一個 process。Node 同時發送與接收（FDD）：node 1 在 2420 MHz 發、2460 MHz 收，node 2
相反（[`configs/profiles/duplex_fdd_2420_2460.yaml`](../configs/profiles/duplex_fdd_2420_2460.yaml)，
備用 `duplex_fdd_2405_2475.yaml`）。一個 Pluto 只能開一個 libiio context（第二個回 EBUSY），所以 TX 與
RX 共用一個 `pluto.SharedPlutoContext`，在同一個 engine 子 process 裡。B210（`--transport uhd`）用同一台
裝置的兩個前端：TX 在一個（預設 RF A，TX/RX port），RX 在另一個（預設 RF B），各是一個 UHD block，
以 `serial=` 選裝置（`node_engine.UhdDuplexFactory`）。

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
# USRP B210（gain 是 B210 的放大器 gain，不是 Pluto 的衰減）
UHD_IMAGES_DIR=/usr/share/uhd/images python -m ofdm_message_link.node_app --node-id 1 \
    --transport uhd --headless --serial 30FE782 --tx-gain 65 --rx-gain 30 --duration 120 \
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
| `--transport {udp,pluto,uhd}` | 預設 `udp`（不用 radio）；`pluto` 一台 Pluto；`uhd` 一台 USRP B210（TX、RX 各用一個前端）。`udp` 用 `--udp-host`（預設 127.0.0.1）與下面的 sample ports，`--udp-port` 對 `node_app` 沒有作用 |
| `--overlay` | 預設 `configs/profiles/duplex_fdd_2420_2460.yaml`；必須有 `duplex` 段 |
| `--serial` / `--auto-start` | `pluto`／`uhd`：選哪一台 Pluto 或 USRP；`--headless` 必須給 `--serial`，而且隱含 `--auto-start` |
| `--channel` / `--antenna` | `uhd`：RX 的前端（0 = RF A，1 = RF B，預設 1；TX 用另一個）與 RX port（`TX/RX` 預設，或 `RX2`）；TX 一律 `TX/RX` |
| `--tx-lo-offset HZ` / `--tx-analog-bw HZ` | `uhd`：TX LO 離載波的距離（預設 8e6，`tune_request`；0 = 不偏移）與 TX analog filter。沒有偏移時 TX 的 LO 洩漏落在對方 RX 中心約 2.4 kHz，淹沒 acquisition。其他 transport 給這兩個會被拒絕。TX filter 的預設值依 sample rate 與 LO offset 計算（見下表）；給 `--tx-analog-bw 25e6` 就是 v2.0 以前的設定 |
| `--tx-guard-us US` | `uhd`（v2.1.1 起）：每個 burst 前送的零值保護段，讓前端 key-up 後穩定，單位 µs（0–20000）。**不給時是 sink 自己的 250 µs，和 v2.1.0 完全相同。** 每個 burst 會多佔這段 air time（1000 µs 比 250 µs 多 0.75 ms）。桌上兩台 B210（2026-11-12）：一台閒置後第一個 burst 有 key-up 暫態，1000 µs 讓那個方向的 preamble SNR 中位數高約 12 dB，1300/s 時 air time 約從 77 % 升到 88 %。是否改成預設還沒決定。其他 transport 給這個會被拒絕 |
| `--tx-freq` / `--rx-freq` | 覆寫 profile 的頻率；信號邊緣離開 2400–2483.5 MHz 時警告 |
| `--tx-gain` / `--rx-gain` | Pluto／udp：TX −89.75–0 dB（衰減），預設 −89.75 / 15 dB。B210（`uhd`）：TX 0–89.75 dB、RX 0–76 dB，預設 0 / 30 dB；兩種尺度不能互用。`--gain` 會被拒絕（node 有兩個 gain） |
| `--rx-analog-bw HZ` | AD9361 RX analog filter，預設 = sample rate |
| `--rx-kernel-buffers N` | libiio RX kernel buffers，預設 4 |
| `--tx-duty-cap D` | `pluto`：TX duty 不超過 D（0.05–1），預設 0.7（USB 2.0 同時帶 5 MS/s RX 時的上限，見 M0）；1 = 不限。`udp` 沒有 duty cap |
| `--peak-amplitude` | 每個 burst 縮放到這個 peak，預設 0.7 |
| `--snr-db` | 只有 `udp`：在 RX 加 AWGN |
| `--snr-fft-advance N` | v2.1.1 起：preamble SNR 估計的兩個 LTF FFT window 提早 N 個 sample（0–8），覆寫 `phy.rx.snr_fft_advance_samples`。**不給時是 0，和 v2.1.0 的 SNR 完全相同。** 只改 SNR 的讀值，不改解碼；但 `olla` 用的 SNR feedback 也會跟著變。原因：硬體上估到的 L1 起點常晚一兩個 sample，L2 的 window 吃到下一個 symbol，`L1 − L2` 把它算成雜訊。桌上兩台 B210（2026-11-12）用快照離線重算，提早 4 個 sample 時 B→A 中位數高約 3 dB、低於 28 dB 的比例從約 13 % 降到 0–5 %。是否改成預設還沒決定。`--events` 的 `start` 行記下用的值 |
| `--headless` | 不開視窗，隱含 `--auto-start`；`pluto` 與 `uhd` 需要 `--serial` |
| `--duration S` | Ready 後 S 秒停止（headless） |
| `--gain-profile "T:DB,..."` | 依 ready 後的秒數排程 TX gain，例如 `0:-5,30:-20,60:-5`；gain 依 `--transport` 的 TX 範圍檢查 |
| `--control-port P` | localhost UDP JSON control socket，預設依 node；0 = 關閉 |
| `--link-adaptation SPEC` | `olla`（預設，兩個 loop，從 `--mcs-index` 開始）、`fixed`（固定在 `--mcs-index`）、`arf`、`pkg.mod:Class`、`file.py:Class` |
| `--link-adaptation-arg K=V` | LA class 的參數，可重複 |
| `--feedback-reporter SPEC` / `--feedback-reporter-arg K=V` | RX 端量測與回報模組，預設 `default` |
| `--mcs-index N` | 起始的 MCS（`fixed` 時是固定的 MCS），預設 8 |
| `--control-mcs-index N` | Control-only burst 的 MCS，預設 profile 的值（8） |
| `--decode-workers N` | 1–64；沒給時取 `acceleration.decode_workers`，`null`（預設）時是 3（兩個 node 在同一台 PC 時的 CPU 預算） |
| `--stats-log PATH` | 每 0.5 s 一行 JSON：`tx`、`rx`、`la`、`feedback`、`cpu`、`device`；control socket 的 `mark` 與 gain profile 的每一步也寫入。`tx.health` 是 TX health（見 7.1 節的 `tx_health`）。`wall_time` 是 Unix 時間（秒），`monotonic_s` 是 monotonic 時間。`tx.goodput_bps`、`tx.airtime_frac` 是 2 秒視窗，`tx.goodput_avg_bps`、`tx.airtime_avg_frac` 是第一行以來的平均。`tx.bandwidth_hz`、`rx.bandwidth_hz` 是 UHD 讀回的類比濾波器頻寬（沒有讀回值時是 `null`）。`--stats-log-max-mb`／`--stats-log-backups` 與 2.7 節相同 |
| `--la-log PATH` | 每個 LA decision 與 feedback report 一行 JSON。v2.1.2 之後，MCS 改變的 `la` 行多一個 `first_sequence`：決定當下下一個要編碼的 DATA sequence，也就是新 MCS 的第一個 DATA subframe（決定的那個 burst 是 control-only 時，是下一個 data burst 的第一個）。所以兩行 `la` 可能有同一個 `first_sequence`：為 control-only burst 做的決定之後，下一個 data burst 又換了一次，或決定之後那個 burst 的 encode 失敗。**同一個 `first_sequence` 以最後一行為準**：從那裡開始的 data burst 用的是最後一行的 `mcs_index`，前面那行的 MCS 沒有送過 data。encode 在分配 sequence 之後才失敗時，最後一行的 `first_sequence` 只出現在 `--events` 的 `tx_dropped`（那些 datagram 沒有送出），沒有對應的 data burst |
| `--events PATH` | 只有 `--headless`：逐 burst 的接收事件，一行一個 JSON（preamble SNR、雜訊變異數、clipping、header／payload／FCS 結果、裝置時間與主機時間），加上 gain、頻率、失鎖、overflow 與每 0.5 s 的計數器；v2.1.2 之後也寫發送端每個送出的 burst（`sent`）與丟掉的 burst（`tx_dropped`）。**預設關閉**，關閉時接收端與發送端的程式路徑不變。格式見 [7.11 節](#711-逐-burst-的接收事件--events) |
| `--events-lock-timeout S` | 配合 `--events`：串流時間連續 S 秒沒有收到 peer 的 header 就算失鎖，預設 1.0 |
| `--ready-file PATH` | 兩個方向都在跑時建立，停止時刪除 |
| `--ingress-port` / `--egress-host` / `--egress-port` | 覆寫依 node 的預設 |
| `--udp-listen-port` / `--udp-peer-port` | `--transport udp` 的 sample ports，預設自己與 peer 的 base+4 |
| `--enable-rf` / `--acknowledgement` | `--transport pluto` 與 `uhd` 必要，和 `tx_app` 相同 |
| Aggregation | `--aggregation`、`--aggregation-max-subframes`、`--aggregation-max-wait-ms`、`--min-burst-interval-ms`、`--max-burst-airtime-ms`，與 `tx_app` 相同（7.1 節） |
| 其他共用的選項 | `--config`、`--mcs`、`--frame-payload-bytes`、`--ui-fps`、`--log-lines`、`--geometry`，與 7.1 節相同 |
| 效能開關 | `--cpu-cores`、`--cpu-affinity`、`--realtime[=PRIO]`、`--force`、`--native-backend`、`--overload-protection`，與 7.1 節相同（7.10 節）；除了 `--cpu-cores`（預設取設定檔，再來是 1）全部預設關閉。`--rx-min-feed-samples`／`--rx-max-merge-wait-ms` 與 `rx_app` 相同 |

**TX analog filter 的預設值（`node_engine.default_uhd_tx_bandwidth`）。** LO 偏離載波 `lo` 時，取樣的頻帶離 LO 最遠是
`|lo| + fs/2`，濾波器的半寬要蓋住它：頻寬 ≥ `2|lo| + fs`，再乘 1.1 的餘量（AD9361 濾波器的 roll-off），進位到整數 MHz，
最小 25 MHz、最大 56 MHz。`tx_app` 不偏移 LO，頻寬等於 sample rate，沒有這個問題。

| Sample rate | 信號離 LO 最遠（lo = 8 MHz） | 舊預設（v2.0 以前） | 新預設 |
|---:|---:|---:|---:|
| 5 MS/s | 10.5 MHz | 25 MHz | 25 MHz（不變） |
| 10 MS/s | 13 MHz | 25 MHz（半寬 12.5 MHz，切到邊緣） | 29 MHz |
| 15.36 MS/s | 15.68 MHz | —（之前不支援） | 35 MHz |
| 20 MS/s | 18 MHz | 25 MHz（切到邊緣） | 40 MHz |

10 MS/s 的結果因此可能與 2026-11-11 的 baseline 不同；硬體驗收會在最好的 10 MS/s 點比較 `--tx-analog-bw 25e6`（舊）與預設（新）。

**Control socket**：每個 datagram 一個 JSON request，回一個 JSON answer。

| Request | 動作 |
|---|---|
| `{"cmd":"set_gain","dir":"tx","db":-20}` | 立即改 gain，回 `{"ok":true,"readback":-20.0}` |
| `{"cmd":"set_mcs","mcs":12}` | 固定 MCS；`"mcs":null` 交回 LA |
| `{"cmd":"set_la","mode":"auto"}` | `auto` 或 `fixed`（凍結目前的 MCS） |
| `{"cmd":"mark","text":"phase 2"}` | 同時寫入 stats log 與 LA log |
| `{"cmd":"stats"}` / `{"cmd":"stop"}` | 最近一行 stats／停止 |

Headless 收到 SIGTERM／SIGINT、`stop` 或 `--duration` 到時：先停 TX（attenuator 回到最小輸出），再停 RX，
刪除 ready file，exit 0；radio 啟動失敗或 control port 已被佔用時 exit 2，engine 子 process 中途結束時 exit 3。

LA 演算法（預設演算法 `olla` 的參數、ladder）與怎麼寫自己的模組見
[3.4 節](03-your-own-app.md#34-寫你自己的-link-adaptation-模組)；設計、自干擾與實機量測見
[`experiments/duplex_fdd/`](../experiments/duplex_fdd/README.md) 與 [8.9 節](08-measurements.md#89-雙向-fdd兩個-node_apppluto--pluto24202460-mhz)。

**USB 限制（M0，`experiments/duplex_fdd/results/usb/`）**：Pluto 同時以 5 MS/s 接收時，TX duty 到
0.7 都沒有 overflow；要求 0.8 以上時 TX 只到 0.78，其中一個 Pluto 每分鐘 overflow 約 180–220 次。
8 個 kernel buffers 沒有改善。

## 7.10 效能開關與 native 模組

**Native 模組。** `pip install -e .`（或 `python setup.py build_ext --inplace`）編譯四個 C++17 extension，
每個只負責一件事，Python 函式的介面都沒變：

| 模組 | 取代的 Python 實作 | 用在 |
|---|---|---|
| `_acq_native` | `sync.acquire_all` 底下兩種 preamble 的偵測：lag-32 相關、DC／tone 的 steady 修正、legacy coherent、v5 M128 gate、NMS、STF coherent、LTF 精細時序與 CFO | Receive owner |
| `_header_native` | v5 header probe：LTF pair 量測、channel smoothing、SIG 等化，交出 SIG LLR（`channel.py` 沒改，workers 照舊） | Receive owner |
| `_turbo_native` | Turbo decoder：新的 float32 kernel（`decode`）；原本的 double kernel 保留為 `decode_reference` | Decode workers |
| `_tx_native` | `rate_match`、`map_symbols`、`modulate_ofdm` | 發送端（仍在 engine 裡） |

全部計算時都會放開 GIL。編譯只用 `-O3 -std=c++17 -ffp-contract=off`，沒有 `-march` 或任何 ISA 旗標，同一份原始碼在
aarch64（Jetson）也能編。

**Backend 開關。** 每一塊都可以選 native 或 Python 參考實作，用環境變數（engine 與 decode workers 會繼承）：

| 變數 | 值 |
|---|---|
| `OFDM_LINK_NATIVE` | `auto`（預設）、`python`、`native`，套用到全部；`python` 會蓋過下面每一個 |
| `OFDM_LINK_NATIVE_ACQ`、`_HEADER`、`_TURBO`、`_TX` | 只設一塊 |

`auto`：模組 import 失敗就退回 Python，警告一次。`native`：import 失敗就報錯。Turbo 的 `python` 是原本的 double kernel
（沒有 Python 版的 max-log decoder）。`--native-backend` 會在任何子 process 啟動前設好 `OFDM_LINK_NATIVE`。等價性由
`tests/phy/test_acq_native.py`、`test_header_native.py`、`test_turbo_fast.py`、`test_tx_native.py` 與
`tests/app/test_native_end_to_end.py` 檢查（容差：起點相同、|ΔCFO| ≤ 1e-6、confidence 相對誤差 ≤ 1e-5、header 欄位相同、
Turbo BLER 差 ≤ 0.1 dB 且 iteration ±1、固定 seed 的端到端 delivered 相同）。

**接收端的其他改變（不需要開關）。** `measure_quality` 改成每軸各自找最近點，16QAM soft demap 改成每軸分開算，輸出與
原本相同。Decoder 的 sample buffer 改成一塊重複使用的記憶體加上起點：feed 進來的 samples 接在後面，丟掉的只移動起點，
不再每次把留下的 samples 整段複製；buffer 只在新 samples 放不下時才搬一次：不夠大時加倍（從 131072 samples 起，最多
`max_input_samples`），長過 4 倍初始大小（524288 samples 以上）之後，留下的 samples 又只剩初始大小的一半以內時才縮回 131072。
每個 sample 進 buffer 時檢查一次是否有限（NaN／Inf 照舊拒絕），之後 acquisition、SNR 量測與 header probe 不再重複檢查；
UHD 與 udp transport 的 samples 一定是有限值（`finite_samples`），連進 buffer 的那次也省掉（udp 在收到 burst 時就丟掉
含 NaN／Inf 的 burst，計數在 transport 的 `bursts_non_finite`，不再像以前那樣讓 feed 報錯；`--snr-db` 小到加上的雜訊
會溢位時，transport 啟動時就拒絕）。解出來的 burst、位置與 SNR 和原本逐位元相同。

**接收端 feed 的大小（`--rx-min-feed-samples`、`--rx-max-merge-wait-ms`，預設不變）。** `ReceiveWorker` 會把 queue 裡已經
在等的短 chunk 併成至少 `rx_min_feed_samples` 個 samples 再 feed，最多為此等 `rx_max_merge_wait_ms`：
命令列 > `acceleration.rx_min_feed_samples`／`rx_max_merge_wait_ms` > 8192 samples、2 ms（PC 的設定；這個值一直沒變，但 UHD 接收端現在也用它在 GNU Radio 裡分組，見下）。
每次 feed 有固定成本，在慢的機器上大一點的 feed 省 receive owner 的 CPU；**Jetson 用 65536 與 15 ms**，設定範例是
[`configs/profiles/jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml)，接在 profile 後面（例如
`--overlay configs/profiles/ota_2p45ghz.yaml --overlay configs/profiles/jetson_rx_feed.yaml`）。`--overlay` 會取代 app
預設的 profile，所以 profile 一定要寫出來，不能只給這個檔。`MessageReceiver`
仍然把每次 feed 切成 8192 samples 的小塊交給 decoder，所以省下的只是每次 feed 的固定成本。samples、交付的 payload
與順序都不變；但小塊是從每次 feed 的開頭切的，feed 的邊界一變，decoder 看到的 buffer 就不同，所以一個 burst 的 SNR 與
雜訊估計可能在最後幾位數不同。啟動時的 `[acceleration]` 那行會印出實際用的兩個值與來源。代價是延遲：一個 burst 最多晚等待的那段時間才交給 decoder，`--events` 的 `mono`
也會跟著晚（7.11 節）。範圍：0–131072 samples（0 = 不併）、0–20 ms。

**Jetson 上的量測（2026-11-19）。** Jetson Xavier NX（MODE_20W_6CORE，沒有跑 `jetson_clocks`）+ B210 與 PC + NI
USRP-2901，OTA，5 MS/s、MCS 12，Jetson `--realtime`、4 個 decode worker（`node_app` 預設是 3，要自己加
`--decode-workers 4`），每點 60 秒。用的是量測 harness 自己的 overlay，不是 repo 裡的 profile：單向 PC→Jetson 在
2460 MHz，PC TX 的 `mac.aggregation.max_wait_ms` 20、`max_burst_airtime_ms` 16；雙向是 `duplex_fdd_2420_2460.yaml`
再加 `min_burst_interval_ms` 20。原始資料在作者本機的 `runs/jetson_b210_20261118/perf_after_item1/`；`runs/` 不進版控。
跟預設的 8192／2 ms 比，65536／15 ms 的遺失（除 200/s 外每格只跑一次）：

| 情境 | 8192／2 ms | 65536／15 ms |
|---|---|---|
| 單向 PC→Jetson，100/s | 0 % | 0 % |
| 單向 PC→Jetson，200/s | 0.25 %、0.29 % | 0 %、0 % |
| 單向 PC→Jetson，300/s | 10.9 % | 10.4 % |
| 雙向 5/5（Jetson→PC／PC→Jetson），PC→Jetson | 0.67 % | 0 % |
| 雙向 50/25，PC→Jetson | 9.2 % | 1.3 % |
| 雙向 100/50，PC→Jetson | 22.6 % | 22.7 % |

雙向時 Jetson→PC 兩種設定每點都是 0 %。遺失上的差別在單向 200/s 與雙向 5/5、50/25；單向 100/s 兩者都不掉，只有
PC→Jetson 延遲中位數變短（239 → 148 ms）。200/s 時 transport 收進 queue 的 chunk（`chunks_received`）每秒約 180 → 72 個，
60 秒內 queue 滿的 back-pressure 約 2.7k–2.9k → 13–134 次，延遲中位數約 323 → 205 ms。飽和點沒有變：單向 300/s、雙向 100/50
兩種設定掉得差不多。單向時 Jetson 6 核約 5.5 核在忙（200/s、0 遺失時已經約 5.3 核），推測瓶頸是總 CPU；雙向 100/50
只有約 3.3 核在忙，瓶頸還沒找到。延遲方面，最多晚 15 ms 是上限：有負載時中位數反而變短（上面的 200/s），雙向 5/5 則
稍微變長（108 → 113 ms）。改善前（`df098bd`，UHD 還沒在 GNU Radio 裡分組）同樣的 8192／2 ms 在 200/s 兩次都是 0 %、雙向
50/25 是 3.7 %（一次）；也就是加了分組之後，預設值在 Jetson 上變差（200/s 0 → 0.25–0.29 %、雙向 50/25 3.7 → 9.2 %，次數少），這也是 Jetson 要另外加上面 overlay 的原因
之一。PC 不用加。

**PC 送往 Jetson 時的 burst 間隔（`--min-burst-interval-ms`，預設不變）。** Jetson 每收一個 burst 都有固定成本，所以送往
Jetson 的那一端可以把 burst 變少、變滿：[`configs/profiles/jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml)
是 Jetson 接收端的設定，[`configs/profiles/tx_to_jetson.yaml`](../configs/profiles/tx_to_jetson.yaml) 是對面 PC 發送端的設定，
只把 `mac.aggregation.min_burst_interval_ms` 從 4 改成 25：一個 burst 開始之後 25 ms 內不送下一個，等的時候繼續裝
datagram。`max_wait_ms` 維持預設的 0。只用在發送端（單向的 `tx_app`，或雙向時 PC 的 `node_app`），一樣接在 profile 後面
（例如 `--overlay configs/profiles/duplex_fdd_2420_2460.yaml --overlay configs/profiles/tx_to_jetson.yaml`）；接收端不讀
`mac.aggregation`，兩台 PC 之間不用加。實際的週期是 25 ms 加上一次編碼的時間（PC 上約 2–3 ms），所以 PC 每秒最多約 36
個 burst（burst 一個接一個時）。

量測（2026-11-19，`efd3a76`）：條件同上一段，Jetson 用 65536／15 ms、4 個 decode worker、`--realtime`，每點 60 秒、每組
3 次（20/s 2 次），單向 PC→Jetson 2460 MHz，雙向 Jetson TX 2420／PC TX 2460 MHz。對照組是量測 harness 原本的 PC TX 設定，
**不是**程式預設：單向 `max_wait_ms` 20、interval 4 ms（`max_burst_airtime_ms` 16）；雙向兩個 node 都是 interval 20 ms。
量測時只有 PC 這一端換成 25 ms，Jetson 的 node 仍是 20 ms（只用 repo 的 `duplex_fdd_2420_2460.yaml` 時 Jetson 的 node
是程式預設的 4 ms，這個組合沒有量過）。程式預設（wait 0、interval 4 ms）這次也沒有量。平均值：

| 情境 | 對照組 | `tx_to_jetson.yaml` |
|---|---|---|
| 單向 20/s，遺失／延遲中位數 | 0 %／127 ms | 0 %／106 ms |
| 單向 200/s，遺失／延遲中位數 | 0 %／188 ms | 0 %／196 ms |
| 單向 300/s，遺失／延遲中位數 | 7.9 %／649 ms | 2.6 %／692 ms |
| 雙向 50/25（Jetson→PC／PC→Jetson），PC→Jetson 遺失 | 0.13 % | 0 % |
| 雙向 100/50，PC→Jetson 遺失／延遲中位數 | 15.5 %／290 ms | 5.2 %／236 ms |

Jetson→PC 兩組都最多 0.03 %。300/s 時 Jetson 的 RX overflow 約 44 → 15 次、雙向 100/50 約 430 → 133 次。代價是有負載時的
延遲：單向 200/s 中位數多 8 ms、300/s 多 43 ms（雙向 100/50 反而少）。20/s 少的 20 ms 來自 `max_wait_ms` 0（datagram 間隔
50 ms 比 25 ms 長，interval 不起作用），程式預設本來就不等。飽和點沒有消失，300/s 仍會掉。同一天稍早在舊的 SDR 擺放
（`035d400`）量到同樣的方向：單向 300/s 10.7 % → 6.2 %（各 5 次）、雙向 100/50 19.9 % → 9.9 %、20/s 中位數少 20 ms、
300/s 中位數多 52 ms。只量了固定 MCS 12。Jetson 自己發送時也用 25 ms 是否有幫助還不確定（舊擺放，各 3 次，範圍重疊），
所以這個檔不給 Jetson 用。原始資料在作者本機的 `runs/jetson_b210_20261118/newsetup_20261119/` 與 `item7/`。

**UHD 接收端在 GNU Radio 裡先分組（ingest batching，跟著上面的 feed 設定，不需要另外的開關）。** 沒有分組時，UHD 每送來
一批 samples，GNU Radio 就呼叫一次 Python 的 RX sink block：B210 在 5 MS/s 約每秒 843 次、每次約 5.9k samples，每次都要拿
GIL 並付固定的 gateway 成本。現在 `ReceiveWorker` 在 transport 啟動前把 feed 設定交給它（`set_receive_feed`），
`UhdSampleSource` 啟動時算出 N，對 sink 設 `set_output_multiple(N)`：GNU Radio 先湊滿 N 個 samples 才交給 Python，
每次交的都是 N 的整數倍（落後時一次拿可用的最大倍數，上限仍是 65536 × ⌈fs / 5 MS/s⌉）。
N = min(`rx_min_feed_samples`, `rx_max_merge_wait_ms` × fs, 上限)；`rx_min_feed_samples` 為 0、或等待時間不到 2 個 samples 時
N = 1（不分組，和以前一樣）。例：PC 預設 8192／2 ms 在 10 MS/s 是 N = 8192（每秒約 1.2k 次）；Jetson
65536／15 ms 在 5 MS/s 是 N = 65536（13.1 ms，每秒約 76 次）；65536／15 ms 在 1 MS/s 是 N = 15000。N 是用裝置回報的實際
sample rate 算的，每次啟動（重新 attach）都重算；GNU Radio 的 buffer 也加大到 2 × 上限 + N。因為 sample 已經在 GNU Radio
裡等過 N 了，`ReceiveWorker` 對分組的 transport **完全不再等**，只把 queue 裡已經在等的 chunk 併進來；所以分組時每次 feed
平常就是 N 個 samples，落後時較多。N 被等待時間或上限限制時，feed 會比 `rx_min_feed_samples` 小：例如 131072／20 ms 在
5 MS/s，上限 65536 讓 N = 65536，feed 就是 65536（不會為了湊 131072 再等）。udp、Pluto 照舊等完整的
`rx_max_merge_wait_ms`。samples 與順序不變，最多晚 N 個 samples（≤ `rx_max_merge_wait_ms`）才交出去；停止時不滿 N 的
最後一段會被丟掉。

每個 chunk 和它裡面的 `rx_time` tag 一起進 queue。遺失的計數（`rx_samples_lost`、`rx_time_discontinuities`）和以前一樣
在 sink 把 chunk 放進 queue 成功之後就算，所以停止時還在 queue 裡的 overflow 也算得到；但 tag 要等 `recv()` 交出那個
chunk 時才成為 device time anchor（`time_anchors()`，`--events` 的 `rx_time`／`overflow`／`dev_t` 用的就是它），所以
anchor 不會早於它描述的 samples 被 decoder 看到。因此 `rx_samples_lost`／`rx_time_discontinuities` 可能比
`time_anchors()` 與 `--events` 的 `overflow` 列早一個 queue 的量（≤ 約 100 ms）；停止時還在 queue 裡的 overflow 會被計數，
但沒有對應的 events 列。sink 因為 queue 滿而重讀同一段時，tag 也不會算兩次。anchor 的 offset
仍是「啟動後交給 `recv()` 的 samples 數」。
`set_receive_feed` 只能在啟動前呼叫（已啟動時拋 `TransportError`），開關在 `transport.RX_BATCH_DEFAULT_FEED`：設成
`False` 時預設的 feed（8192／2 ms）N = 1、PC 的 sink 行為和以前一樣，有設定的 feed（Jetson）照樣分組。

Sink 與 `recv()` 之間的 queue 分組時用 samples 計：queue 裡的 samples 加上新 chunk 超過 ⌈0.1 × fs⌉（裝置實際的 fs，
即 100 ms）就拒收（back-pressure），只有 queue 空的時候一定收下一個 chunk（所以最多是 100 ms 或一個 chunk，取大者）。
不用 chunk 數計，是因為 back-pressure 之後 sink 每次會拿到上限那麼多，而不是 N。不分組時照舊最多 16 個 chunk。
transport 快照多了 `ingest_multiple`（N）、`queue_budget_samples`（上面的上限，不分組時是 None）、`queued_samples`
與 `queue_depth_chunks`（chunk 數上限，分組時是 None）。**計數的尺度變了：** `chunks_received` 是交給 queue 的 chunk 數，
分組後約少 N／5.9k 倍；`queue_full_backpressure_events` 是 sink 遇到 queue 滿、等 5 ms 後退回的次數，queue 從約 19 ms
變成 100 ms 之後，同樣的停頓比較晚、比較少觸發它，所以和之前版本的數字不能直接比較（`judge_run.py` 只看它是否持續
增加，判定方式不變）。

**NumPy thread 數與 decode workers（`acceleration`，預設不變）。** NumPy 內建的 BLAS 會依機器的核心數開 thread pool，
而且在呼叫之間空轉；在 20 核心的機器上它吃掉 15.6 個核心、少了三分之一的 throughput。所以每個 app 的主 process 與
engine 子 process 都會把已載入的 BLAS／OpenMP 函式庫限制在 `cpu_cores` 個 thread（`ofdm_link/runtime/cpu_budget.py`）：
`--cpu-cores` > `acceleration.cpu_cores` > 1。`main` 在 `--cpu-affinity` 之後才套用（所以 0 算的是綁定後的 CPU，超過 256 時取
256），再把值當參數交給 engine 子 process；不經過環境變數，所以 shell 裡殘留的 export 或已經在跑的 forkserver 改變不了它。
沒有 app `main` 的 process（例如 `experiments/tx_v5/rx_throughput_bench.py`）自己傳 1。Decode workers 不管這個值，固定各 1 個 thread：平行度來自 worker 數，
`--decode-workers` > `acceleration.decode_workers` > app 預設（`rx_app` 4、`node_app` 3）。兩個 key 的範圍見 7.2 節。
Receive path 沒有大到能從多個 BLAS thread 得到好處的線性代數，所以 `cpu_cores` 調大通常不會更快；它是限制浪費的上限。

Decode worker 在一個 burst 上丟出 `BurstDecodeError` 以外的例外（程式錯誤）時，只有那個 burst 算失敗：記成無法分類的
payload 失敗（`unclassified_decode_failures`，和 inline decoder 解不出又分不出原因的 payload 一樣），後面的 burst 照順序繼續交付，
錯誤訊息每次 feed 合成一行 `failed`（`decode worker raised on N burst(s): ...`），pool 快照的 `worker_errors` 計數。整個 task 的結果
送不回來（例如不能 pickle）時，那個 task 的每個 segment 都這樣算。worker 死掉（`BrokenExecutor`）或 pool 不再接受工作時，pool 關閉並丟出
`DecodePoolError`。以前一個這樣的例外會一直卡在 pool 的最前面，之後每次 feed 都重丟一次，後面的 burst 全部遺失。
等待結果逾時（`flush(timeout)`）不算失敗：那個 task 留在最前面，晚一點照常交出結果，即使它剛好在逾時之後才解完。
Decode worker 忽略 SIGINT：終端機的 Ctrl-C 會送到整個 process group，worker 何時停由擁有它的 receiver（`close()`）決定，
不會在串流中途變成某個 task 的 `KeyboardInterrupt` 或讓 pool 壞掉。

**Engine 子 process 交給母 process 的事件分批送（不需要開關）。** 視窗與 `node_app` 要的事件（`message`、`symbols`、
`link_snr`、`sent`、`la`、`feedback`、`bursts`、`stats`、`gain`、`mark`、`failed`……）以前每個事件 put 一次
multiprocessing queue；每次 put 都要叫醒 queue 的 feeder thread，在 GIL 下 pickle 再寫 pipe。Jetson 上 profiling 量到這個
feeder thread 佔 engine GIL 時間的 18.5 %（約每秒 280 個小事件，每個約 0.4 ms）。現在子 process 的每個 thread（接收、發送、
LA controller、主迴圈的 stats tick 與 gain／mark／failed、detach 時 `--events` 的最後一批）都放進同一個有鎖的 buffer，照放入
的順序；在下面幾個時間點把整個 buffer 當成 queue 的一個 item 送出：每個工作單位結束時（一次 receive iteration，包括中途
`continue` 的；一個 TX burst 或一次 idle 等待；一個 control 命令；一次 stats tick）、buffer 到 64 個事件時、每一個回覆母
process 的 reply 之前（attach、detach、`gain_sync`）、`_serve` 迴圈的每一輪（最多約 50 ms 一次），以及停止時。母 process 的
pump 拆開每一批，照原來的順序一個一個交出去，所以跨 thread 的順序也不變。

不變的：UDP egress 照舊由接收 thread 在解出 datagram 時依序直接送出，不經過這個 queue；link adaptation 在子 process 裡，
不受影響。Queue 的上限仍是 4096 個**事件**（不是批）；放不下的那一批整批丟掉，`dropped_display_events`（以及 `--events`
`status` 行的 `dropped_events`）計的是丟掉的事件數，和以前的意思相同。這個上限不靠兩個 process 共用的鎖：子 process 自己
記送出的事件數，母 process 只寫它取走的事件數（一個沒有鎖的共享整數），子 process 只讀；所以母 process 的 pump 拿不到 GIL、
被排程延後或整個掛掉，都不會讓子 process 的接收或發送 thread 等它（之前的版本共用一個 `multiprocessing.Value` 的鎖，母 process
主 thread 忙著時，子 process 的一次 put 量到最多約 27 ms）。一批裡有某個值不能 pickle 時，只丟那個事件：同一批其他事件照原位置
送出，丟掉的算進 `dropped_display_events`，stderr 印一行 `[engine-events] dropped N event(s) that cannot be pickled`，它占的
上限也還回來（之前會丟掉整批，而且上限永遠少掉那一批，最後所有事件都被丟掉）。

會變的：母 process 收到事件的時間最多晚一次 flush 的間隔。接收 thread 的事件晚到這次 iteration 結束（通常就是這次 feed
剩下的處理時間）；發送 thread 的晚到這個 burst 送出；沒有自己 flush 的 thread（從其他 thread 呼叫的 controller 等）最多晚約
50 ms 加一次 stats tick；主 thread 被一個很久的 attach 卡住時，其他 thread 的事件仍各自送出。母 process 用「收到的時間」記錄的
值會跟著晚這麼多：`node_app` 的 RX SNR 曲線（`link_snr`）、LA 歷史（`stats` 收到的時間），以及 `--events` 檔裡由母 process
記時間的列（`failed`、`gap`、`lock`、`rx_time`、`overflow` 的 `mono`／`wall`）。`burst`、`sent`、`tx_dropped` 列的
`mono`／`wall`、`gain` 與 `mark` 的 `mono` 是子 process 記的，不受影響。

PC 上的 micro-benchmark（`experiments/engine_events/bench_event_batching.py`，子 process 每次 iteration 送幾個接收端的事件，量子 process 的 CPU
時間，含 feeder thread，減去只建事件不送的 baseline；每秒 280 個事件、10 s、各 3 次的中位數，2026-11-19）：

| 每次 iteration 的事件數 | 每個事件 put 一次（以前） | 每次 iteration 一批（現在） |
|---|---|---|
| 1 | 96 µs | 100 µs |
| 4 | 83 µs | 70 µs |
| 16 | 33 µs | 23 µs |

成本大部分是每次叫醒 feeder thread 的固定成本，所以省多少看每批有幾個事件；一個 iteration 只有一個事件時沒有差別。
PC 上模擬 CPU-bound 的 receive owner（每次 iteration 10 ms 純 Python 運算、4 個事件、每個 message 後一次 UDP `sendto`）時，
兩種做法 owner 完成的工作量都和不送事件時差不到 1 %，看不出差別；Jetson 上 GIL 時間實際省多少還沒有量。
母 process 主 thread 一直佔著 GIL 時（`busy-parent`，每 5 ms 一次 iteration、4 個事件，各 3 次）：子 process 一次 iteration
的 put 最長 0.60–1.17 ms（以前）與 0.44–0.87 ms（現在），沒有等母 process 的情形；以前每個事件一個 item，搶不到 GIL 的
pump 跟不上，3 次裡有 2 次 queue 滿而丟掉 397 與 1742／8004 個事件，現在 3 次都是 0。

**CPU 放置與排程（全部預設關閉）。**

- `--cpu-affinity CPUS` 在 `main` 一開始就套用，engine、forkserver、decode workers 都繼承。不寫死任何核心編號。
- `--realtime[=PRIO]`（`ofdm_link/runtime/realtime.py`）：engine process 的每個 thread 改成 SCHED_RR，加上
  SCHED_RESET_ON_FORK，所以 decode workers 與之後產生的 process 都是一般排程；radio attach 之後新開的 thread 會補上。
  優先權限 1–49，低於 kernel 的 IRQ thread（50）。不會關閉 RT throttling（`sched_rt_runtime_us`）。可用 CPU（套用
  `--cpu-affinity` 之後）少於 4 個時拒絕，除非 `--force`；非 Linux 或沒有權限（`ulimit -r`）時不做事，只記一行 log。
  離線測試（2 個 E-core）中整棵 process tree 用 `chrt -f 50` 反而遺失變多，其他程式最多卡 0.95 s，所以只開到這裡。

**過載保護 `--overload-protection`（預設 `off`）。** 只有在 CPU 不夠、寧可讓遺失平緩增加也不要整個崩潰時才用：

| 模式 | 做什麼 | 計數器 |
|---|---|---|
| `drop` | Decode pool 滿時，丟掉放不下的 segment，receive owner 不等 | `decode_pool.dropped_full` |
| `give-up` | Turbo block 到第 4 次 iteration 時，hard decision 還有超過 1/64 在變，就停止（reason `abandoned`） | decoder 的 `payload_abandoned` |
| `both` | 兩者都開 | 兩者 |

被丟掉或放棄的 burst **不算**進 `payload_decode_attempts`、`payload_decode_failure`、`crc_failures`、`psdu_fcs_failures`，
所以遺失與 BLER 的定義在保護關閉時不變。**量測（包含 JMS1 的 SNR／EVM 與遺失量測）一律關閉**；打開時程式會印一行提醒。

## 7.11 逐 burst 的接收事件（`--events`）

給量測用的正式輸出（v2.1.0 起）。`node_app --headless --events node2_events.jsonl` 把接收端看到的每一個
preamble 寫成一行 `burst`，同時配上這個 burst 的解碼結果，另外寫 gain、頻率、失鎖、overflow 與計數器。
發送端（v2.1.2 之後）每個被 transport 接受的 burst 寫一行 `sent`，每個 encode 或 send 失敗而丟掉的 burst 寫一行
`tx_dropped`，所以 TX 與 RX 兩個檔可以一個 burst 對一個 burst 接起來（見下面〈發送端〉）。
**預設關閉**；關閉時 decoder 與 receiver 每個 burst 只多一次屬性檢查（`burst_observer is None`），transmit worker
每個 burst 只多幾次 `events is None` 檢查與整數計數，沒有其他程式路徑改變。程式在 `src/ofdm_message_link/burst_events.py`：子
process 的 `BurstEventRecorder`（接在 decoder 與 `MessageReceiver` 上）與 `BurstEventTap`（接收 observer），母 process
的 `EventLogWriter`（寫檔）；發送端的紀錄在 `tx_app.TransmitWorker`（`events=`）與它送出的 `SentRecord`。
同一份資料也可以不寫檔、直接拿：`NodeSpec(burst_events=True)` 的 `ProcessNodeWorker` 會把 `bursts` 事件交給
listener callback（見 `node_engine.ProcessNodeWorker`），或在自己的程式裡呼叫
`MessageReceiver.enable_burst_events(recorder)`。讀檔用 `burst_events.read_events(path)`（最後一行寫到一半時跳過）。

**每一行**都有 `k`（種類）、`node`、`mono`（主機 `time.monotonic()`）、`wall`（主機 `time.time()`）。格式版本在
`start` 行的 `schema`（`ofdm-message-link/events`）與 `version`（目前 1）；欄位只會增加，改名或改意思時版本加一。

| `k` | 什麼時候 | 欄位 |
|---|---|---|
| `start` | 開檔時 | `version`、`sample_rate`、`burst_fields`、`package_version`、`argv`、`peer_id`、`transport`、`serial`、`tx_freq_hz`、`rx_freq_hz`、`tx_gain_db`、`rx_gain_db`、`tx_lo_offset_hz`、`tx_bandwidth_hz`、`mcs_index`、`link_adaptation`、`lock_timeout_s`、`clipping_level`；v2.1.1 起加 `snr_fft_advance_samples`（`snr_db` 用的 FFT window 提早量，0 = v2.1.0 的估計；v2.1.0 的檔案沒有這個欄位，當時一律是 0）與 `tx_settle_guard_s`（B210 每個 burst 前的零值保護段，秒；預設 0.00025，其他 transport 是 `null`。v2.1.0 的檔案沒有這個欄位，當時一律 250 µs） |
| `burst` | 每個 preamble 一行（下表） | |
| `lock` | 失鎖與重新鎖定 | `state`（`acquired`／`lost`）、`off`、`stream_t`（= `off / sample_rate`）、`dev_t`（v2.1.1 起：轉換時的裝置時間，沒有 `rx_time` 的 transport 是 `null`；v2.1.0 的檔案沒有這個欄位） |
| `rx_time` | UHD 的 `rx_time` tag（開始時一個，每次 overflow 或 retune 一個） | `off`、`dev_t`：這個 sample 的裝置時間 |
| `overflow` | 接收串流有缺口 | `source = "rx_time"`：`off`、`dev_t`、`lost`（遺失的 samples，由 `rx_time` 算出）；`source = "counter"`：`counter`（`uhd_rx_overflow`、`chunks_dropped_overflow`、`rx_overflow_events`）、`delta`、`total` |
| `gain` | 每次 gain 變更（control socket、`--gain-profile`） | `dir`、`db`（要求值）、`readback`（裝置讀回）；`mono` 是子 process 套用的時間 |
| `radio` / `readback` / `freq` | 第一行 stats 時記下 TX／RX 頻率、gain、類比濾波器；`readback` 是 UHD 讀回的設定；之後頻率改變時寫 `freq` | |
| `status` | 每 0.5 s（跟 stats log 同一行的時間） | `tx`（`bursts`、`control_bursts`、`subframes`、`payload_bytes`、`active_mcs`）、`rx.decoder`、`rx.receiver`、`rx_samples_lost`、`rx_time_discontinuities`、`uhd_faults`、`dropped_events`、`lost_batches` |
| `sent` | 發送端每個被 transport 接受的 burst（data 與 control-only 都寫；v2.1.2 之後） | 見下面〈發送端〉 |
| `tx_dropped` | 發送端丟掉的 burst：encode 失敗，或 send 失敗而不再重送（v2.1.2 之後） | 見下面〈發送端〉 |
| `mark` / `failed` | control socket 的 `mark`、引擎回報的錯誤 | `text` |
| `gap` | 子 process 到母 process 的事件 queue 滿了，掉了幾批 burst | `lost_batches`、`next_seq` |
| `stop` | 關檔時 | 各種行的數量、`lost_batches`、`write_errors` |

**`burst` 的欄位**（順序同 `burst_events.BURST_FIELDS`）：

| 欄位 | 意思 |
|---|---|
| `off` | preamble 第一個 sample 在接收串流中的位置（從 RX 開始算的 sample 數） |
| `dev_t` | 那個 sample 的裝置時間（秒）：最近一個不晚於它的 `rx_time` anchor 加上 `(off − anchor) / sample_rate`。只有 UHD 有；B210 的裝置時鐘從開機起算，沒有對主機或 GNSS 對時 |
| `mono`、`wall` | 接收 thread 把含有這個 preamble 的 chunk 交給 decoder 時的主機時間。比 burst 在空中的時間晚一段 queue 與 USB 的延遲（沒有量），再加上併 chunk 的等待：最多 `rx_max_merge_wait_ms`（預設 2 ms，Jetson 設定 15 ms：[`jetson_rx_feed.yaml`](../configs/profiles/jetson_rx_feed.yaml)，見 7.10 節） |
| `snr_db` | preamble（v5 是 LTF pair）量到的 SNR，換算到 payload samples，和 `link_snr` 事件同一個值。兩個 FFT window 提早 `start` 行的 `snr_fft_advance_samples` 個 sample；不同值的檔案不能直接合併比較 |
| `noise_var`、`signal_power` | 同一個量測的複數雜訊變異數（每個 sample）與 active carriers 上的平均 `|H|²`；都是線性值，接收的 full scale 是 1.0 |
| `peak`、`clip` | burst 的 samples 裡最大的 `|I|` 或 `|Q|`（header 失敗時只看 fixed header 那一段）；`clip = peak ≥ 1900/2048`（與 `rx_app` 相同） |
| `hdr` | `ok`（CRC 檢查過的 header）、`fail`（header 解碼失敗）、`invalid`（找到 preamble，但因為其他原因沒有可用的 header） |
| `pl` | `ok`、`fcs_fail`（v5 PSDU 的 FCS 失敗，CRC 正確的 subframes 仍會交付）、`fail`（沒有 payload）、`abandoned`（過載保護放棄）、`dropped`（過載保護沒有解）、`null`（header 不是 `ok`，沒有嘗試 payload） |
| `kind` | payload 解出來時：`data`（至少一個自己 link 的 DATA frame）、`control`（只有 control frame）、`own`（自己 TX 的洩漏，被丟掉）、`foreign`（CRC 正確但不是我們的）、`other` |
| `mcs`、`seq`、`sf_ok`、`sf_bad`、`lost`、`fcs` | burst 的 MCS、第一個 DATA sequence、CRC 正確與錯誤的 subframes、這個 burst 之前不見的 DATA sequence 數、PSDU 的 FCS 是否通過。header 失敗或 payload 沒解出來時是 `null`（MCS 也是，header 的 MCS 沒有往外送） |

**失鎖的定義**：連續 `--events-lock-timeout`（預設 1 s）沒有一個 `hdr = ok` 且 `kind` 不是 `own` 的 burst，就寫一行
`lock lost`；下一個這樣的 burst 寫 `lock acquired`。peer 沒有負載時每 0.5 s 左右仍送一個 control burst，所以 1 s
不會把閒置的 link 算成失鎖。時間用接收串流本身的時間，不是主機時間：

- 有 `rx_time` 的 transport（UHD）從 v2.1.1 起用**裝置時間**計時，所以接收端自己 overflow 遺失的 samples 也算在內。
  沒有缺口時 `lost` 的 `off` 是 `最後一個 burst 的 off + timeout`，和 v2.1.0 相同；失鎖的時間點落在 overflow 缺口裡時，
  `off` 是缺口後的第一個 sample（串流裡沒有更早的位置），`dev_t` 是 `最後一個 burst 的 dev_t + timeout`。
- 沒有 `rx_time` 的 transport（udp、Pluto）用串流位置 `off` 計時，和 v2.1.0 相同。
- 一個 header 已經解出來、payload 結果還沒回來的 burst（decode pool 還在解，或 samples 還沒到齊），可能就是 peer 的，
  所以 v2.1.2 起，只要它在上一個 peer burst 之後 timeout 以內，就先不判失鎖，等它的結果；最多等到串流超過它一個
  timeout。這只改變 `lost` 那一行寫出的時間（`mono`），`off` 與 `dev_t` 仍以最後一個 peer burst 計算。v2.1.1 沒有等：
  接收端停住 2 s 一次會寫出 2–5 組 `lost`／`acquired`（桌上量測 `v211_stalltree_r1`、`r2`），只有最後一組是對的，
  多出來的 `acquired` 的 `mono` 也比前一行 `lost` 早。分析 v2.1.1 的檔案時，同一個缺口的 `lost` 只取最後一個、
  `acquired` 只取 `off` 在缺口之後的那個。
- v2.1.0 只用 `off` 計時：overflow 遺失的 samples 不算進 `off`，所以接收端自己停住超過 timeout 也不會寫 `lock lost`
  （桌上量測 `v210_stalltree_r2` 發現）。分析 v2.1.0 的檔案時，要用 `overflow` 行的 `lost` 自己補判。

**發送端（v2.1.2 之後）。** `sent` 的 `mono`／`wall` 是 transmit worker（子 process 的發送 thread）在 transport 的
`send` 傳回「接受」之後立刻記的時間，不是母 process 收到的時間。接受的意思依 transport 不同：udp 是 burst 的所有 UDP
datagram 都交給了 socket；UHD 是 burst 連同它的裝置開始時間排進 sink 的 queue（真正上空中還要再等 queue 前面的
burst 與 lead time）；Pluto 是 push 傳回（大約一個 burst 的 airtime）。

| 欄位 | 意思 |
|---|---|
| `n` | 這個 node 第幾個被接受的 burst（從 1 起算，data 與 control-only 一起數）。跳號表示中間的 `sent` 行在事件 queue 滿時被丟掉了 |
| `first_sequence` | 第一個 DATA subframe 的 16-bit sequence；control-only burst 是 `null` |
| `subframes` | DATA subframe 數。下一個 data burst 的 `first_sequence` 是 `first_sequence + subframes`（mod 65536），除非中間有 `tx_dropped` |
| `control_subframes`、`control_sequence` | 放在 data 前面的 CONTROL subframe 數與第一個的 sequence（CONTROL 有自己的計數器；沒有 CONTROL 時 `control_sequence` 是 `null`） |
| `mcs` | 編碼用的 MCS index |
| `planned_mcs` | aggregation 等待（`max_wait_ms` 或 `min_burst_interval_ms`）估算這個 burst 能裝多少時用的 MCS：policy 的 `peek_next_entry`，沒有 policy 或它還沒有答案時是 TX 目前的 MCS。真正裝幾個仍由 encode 用的 `mcs` 決定；兩者不同只表示 MCS 在等待與 encode 之間換了（等待可能多等或少等），burst 不會因此超過上限，也不會掉 datagram。沒有等待可估時是 `null`：control-only burst、aggregation 關閉、兩個等待都是 0；`max_wait_ms` 是 0 時，啟動後的第一個 burst 也是（`min_burst_interval_ms` 從上一個 burst 算，那時還沒有上一個）。`max_wait_ms` 大於 0 時每個 data burst 都有值 |
| `sample_count` | burst 的 samples，含前後 guard |
| `control_only`、`standalone` | 只有 CONTROL frame；`standalone` 是 policy 為了 feedback 在排隊的 data 之前先送的 control-only burst（閒置時的 control burst 是 `false`） |
| `attempt` | 第幾次交給 transport 時被接受：1 是第一次；大於 1 表示前面的 send 失敗而保留重送（`retry_failed_send` 的 transport），或 radio 在接受它之前被 detach，換了 radio 後再送。重送的是同一份 samples、同樣的 sequence 與 MCS，所以一個 burst 仍只有一行 `sent` |

`tx_dropped` 的欄位：`reason`（`encode_failed` 或 `send_failed`）、`first_sequence`、`subframes`、`control_subframes`、
`control_sequence`、`mcs`、`attempt`（`send_failed` 是最後一次失敗的 attempt；`encode_failed` 是 0）。`encode_failed` 的
`control_sequence` 一律是 `null`（失敗的 encode 可能已經用掉 CONTROL sequence，但沒有記下來；`control_subframes` 是要放的
CONTROL 數）。`mono`／`wall` 是丟掉
的時間。`send_failed` 時這些 sequence 已經用掉，接收端會看到這段缺口；`encode_failed` 時 encode 已經分配 sequence 才失敗的話
`first_sequence` 是用掉的第一個，在分配之前就失敗時是 `null`（沒有用掉 sequence，接收端看不到缺口），`subframes` 都是丟掉的
datagram 數。

**TX 與 RX 對起來**：peer 檔裡 `kind = "data"` 的 `burst` 行，`seq` 等於這邊 `sent` 的 `first_sequence`，`mcs` 等於 `mcs`，
沒有損壞時 `sf_ok` 等於 `subframes + control_subframes`（`sf_ok`、`sf_bad` 也算 CONTROL subframe）。兩邊的 `mono` 是不同主機的
時鐘，不能直接相減；同一台主機時（udp）`burst` 的 `mono` 也可能比 `sent` 早一點，因為 send 傳回之前接收端就可能已經收到。

**順序與完整性**：同一批（一次 receive feed）裡的 `burst` 依 `off` 排序；decode pool 的 payload 比較晚回來，所以
跨批次時要再依 `off` 排一次。每批有編號，queue 滿而掉的批次寫成 `gap`，不會默默消失。停止時（detach）會把還沒有
結果的 burst 照當時的狀態寫出。

**對效能的影響（2026-11-12，同一台電腦、一個 CPU、同一段 240 個 MCS 16 burst 的串流，`MessageReceiver.feed` 的
CPU 時間，各 5 次的中位數，兩輪）**：關閉時 0.412 與 0.398 s，改動前 0.411 與 0.410 s，差異在雜訊內；打開時
0.415 與 0.399 s。B210 上（`--realtime`、MCS 16、兩個方向各 1300 個 datagram／s、60 s）開與關也看不到代價：v2.1.0
關兩次遺失 0、開兩次遺失 0 與 18／78000（RF 造成的 FCS 失敗），四次都沒有 overflow 與遺失樣本，開的兩次也沒有 `gap`；v2.1.1 開六次
也沒有 overflow、遺失樣本與 `gap`（PRISM `experiments/jms1_bench/README.md` 第 4 節與〈2026-11-12 第二輪〉）。

回到 [README](../README.md)。

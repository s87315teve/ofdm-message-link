# 3. 接上你自己的程式

這條 link 對應用程式來說就是兩個 UDP port：把 bytes 送進 TX 的 `:52001`，從 RX 的 `:52002` 拿出來。
你的程式不需要知道底下有 OFDM。

## 3.1 最小範例

發送端一直開著 UDP ingress（預設 `127.0.0.1:52001`），接收端一直把解出來的訊息轉發到
`127.0.0.1:52002`。先照 [README](../README.md#五分鐘上手不需要硬體) 把 `rx_app` 與 `tx_app` 開起來，
再開兩個終端機：

```bash
# 終端機 3：消費者
python -m ofdm_message_link.udp_recv

# 終端機 4：生產者
python -m ofdm_message_link.udp_send "Hello over OFDM!"
python -m ofdm_message_link.udp_send '{"altitude": 150.3, "velocity": 18.7}'
python -m ofdm_message_link.udp_send --file /path/to/photo.jpg
```

自己寫也只要幾行：

```python
import socket
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"hi", ("127.0.0.1", 52001))
```

[`udp_send.py`](../src/ofdm_message_link/udp_send.py) 與
[`udp_recv.py`](../src/ofdm_message_link/udp_recv.py) 都很短，可以直接當你程式的起點。

## 3.2 設計你的應用時要知道的三件事

1. **一個 UDP datagram 是一則 message。** 大於單一 frame payload（預設 968 bytes）的 message 會自動
   fragment 成多個 burst，並在接收端 reassemble。所以文字、JSON、二進位檔都走同一條路徑。
2. **不完整的 message 會被丟棄並計數，不會截斷後交付。** 單向 link 沒有辦法去要回缺掉的 fragment。
   Message 越大、fragment 越多，任何一個 burst 掉了整則就沒了。對 loss 敏感的資料請盡量讓每則
   message 小於 968 bytes。
3. **發送端不會告訴你丟包。** 送得比 link 能承載的還快時，多出來的 datagram 會在 TX 的 ingress
   queue（256 格）被默默丟掉，只能從接收端的序號缺口看出來。請在你的程式裡自己限速。每種 MCS
   的上限見 [throughput 實驗報告](../experiments/ota_udp_throughput/README.md)。

## 3.3 Webcam 影像串流

```
webcam → ffmpeg ─UDP :52001→ tx_app ─channel→ rx_app ─UDP :52012→ ffplay
```

影像是很好的 demo：掉一個 packet 畫面就破一塊，link 品質一眼就看得出來。

### 先用模擬跑（不需要 SDR）

開四個終端機，每個都先 `conda activate ofdm-message-link`。

```bash
# 終端機 1：接收端（先開）；解出的 datagram 轉發到 52012
python -m ofdm_message_link.rx_app --snr-db 20 --egress-port 52012

# 終端機 2：發送端
python -m ofdm_message_link.tx_app --mcs-index 4

# 終端機 3：播放端
ffplay -fflags nobuffer -flags low_delay -framedrop \
    "udp://127.0.0.1:52012?fifo_size=100000&overrun_nonfatal=1"

# 終端機 4：攝影機 → H.264 CBR + intra-refresh → MPEG-TS 752-byte datagram
ffmpeg -nostdin -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15 -i /dev/video0 \
    -vf format=yuv420p -c:v libx264 -preset ultrafast -tune zerolatency \
    -b:v 1300k -maxrate 1300k -minrate 1300k -bufsize 650k \
    -x264-params "nal-hrd=cbr:force-cfr=1:intra-refresh=1:keyint=30:repeat-headers=1" \
    -muxrate 1440k -f mpegts "udp://127.0.0.1:52001?pkt_size=752"
```

停止順序：ffmpeg → TX app → RX app → ffplay，各按 Ctrl-C。

沒有 webcam 的話，把 `-f v4l2 ... -i /dev/video0` 換成 `-re -f lavfi -i testsrc=size=1280x720:rate=15`
就會送出測試圖樣。`v4l2-ctl --list-formats-ext` 可以查你的攝影機支援哪些格式。

### 參數為什麼這樣設

- **`pkt_size=752`**：一個 UDP datagram 走一個 PHY burst，所以必須小於單一 frame 可攜帶的
  968 bytes。752 是 4 個 188-byte 的 MPEG-TS packet。
- **固定 bitrate**（`-b:v`、`-maxrate`、`-minrate`、`nal-hrd=cbr`、`-muxrate`）：單向 link 沒有重傳，
  遺失一個 datagram 就是一塊破圖，所以要讓發送速率平穩、而且留足餘裕。
- **`intra-refresh=1`**：把 I-frame 攤到每張 frame 上。否則每次 I-frame 會一次湧入幾十個 burst。
  破圖之後畫面也會在約 2 秒內自行恢復。
- **`-nostdin`**：一定要加。在背景執行時 ffmpeg 若讀終端機會被暫停（`ps` 狀態 `T`），看起來在跑，
  其實一個 packet 都不送。
- **`52012`**：`52002` 是 `udp_recv` 範例的預設 port；影片改用 `52012` 就不會和它搶。

### 選 MCS 與 bitrate

以下是實機 OTA 的量測（B210 → NI USRP-2901，5 MS/s），可以當作起點：

| 接收端 `SNR (2 s)` | 設定 | 752-byte datagram 的上限 | 建議影片 bitrate |
|---|---|---|---|
| ≥ 11 dB | **MCS 4**（16QAM Turbo 1/3） | 2.41 Mbit/s（mux rate） | **1.3 Mbit/s**（mux 1.44，約 60%） |
| 約 8–11 dB，或 MCS 4 出現 `bursts missing` | **MCS 0**（QPSK Turbo 1/3） | 1.38 Mbit/s | 0.75 Mbit/s（mux 0.83） |

SNR 低於 11 dB 請退回 `--mcs-index 0`（Link light 會提示 `try MCS 0`）。長時間請讓 bitrate 留在
上限的 60% 左右。

### 用真的 SDR：一鍵腳本

> ⚠️ 這會讓 SDR 發射。請先讀完[用真的 SDR 發射](04-ota-hardware.md)，並且已經用訊息 demo 確認過
> 兩台 radio 之間的 link 是通的。

```bash
scripts/run_ota_video_demo.sh --enable-rf                  # 在 GUI 選硬體，一直跑，Ctrl-C 停止
scripts/run_ota_video_demo.sh --enable-rf --duration 180   # 影片開始後 3 分鐘自動停止
scripts/run_ota_video_demo.sh --enable-rf --layout none    # 不排列視窗

# 兩台 ADALM-Pluto（serial 必填；gain 是 Pluto 的範圍）：
scripts/run_ota_video_demo.sh --enable-rf --transport pluto --auto-start \
    --tx-serial <TX_PLUTO_SERIAL> --rx-serial <RX_PLUTO_SERIAL> \
    --tx-gain -10 --rx-gain 15 --mcs-index 0 --video-kbps 750
```

腳本做的事：

1. 檢查有沒有上一次留下來的 process，並用 `xrandr` 決定視窗排列。
2. 同時開啟 RX 和 TX 兩個視窗，兩者都停在 `Hardware` tab。Radio 還沒啟動，**不會有任何發射**。
3. 你在每個視窗的 Radio panel 選好裝置、antenna、gain，按 **Start radio**（先 RX、後 TX）。
4. 等到兩邊的 radio 都啟動之後，才開啟 ffplay 並開始 ffmpeg 串流。
5. 結束時（時間到、Ctrl-C 或任何錯誤）一律依 ffmpeg → TX → RX → ffplay 的順序關閉。

常用選項：

| 選項 | 預設 | 用途 |
|---|---|---|
| `--enable-rf` | 必填 | 確認這會讓 SDR 發射；沒帶就拒絕執行 |
| `--transport T` | `uhd` | `uhd`（USRP）或 `pluto`（ADALM-Pluto） |
| `--overlay PATH` | app 的預設 | 傳給兩個 app 的設定 profile，可重複 |
| `--auto-start` | 關 | 不等你選，直接用 `--tx-serial`／`--rx-serial` 指定的裝置啟動 |
| `--duration SEC` | 不限 | 影片開始後 SEC 秒自動停止 |
| `--mcs-index N` | 4 | SNR 不夠時改 0。TX 視窗的 MCS 選單也能現場切換 |
| `--video-kbps N` | 1300 | 影片 bitrate；MCS 0 請用 750 |
| `--tx-serial` / `--rx-serial` | 無 | 視窗中預選的裝置 |
| `--tx-gain` / `--rx-gain` | 裝置最低值 / 15 | 預選的 gain（dB），執行中仍可在視窗裡調整 |
| `--camera DEV` | `/dev/video0` | 攝影機裝置 |
| `--layout MODE` | `auto` | 視窗排列：`auto`、`single`、`dual`、`none` |
| `--out DIR` | `/tmp/ofdm_ota_video_demo/<時間>` | log 與 `rx_stats.jsonl` 存放位置 |

### 視窗排列

| 模式 | 排法 |
|---|---|
| `dual` | 螢幕 1 整個放 TX；螢幕 2 上方 2/3 放 ffplay、下方 1/3 放 RX |
| `single` | RX 佔主螢幕左半邊全高；右上 ffplay、右下 TX |
| `auto`（預設） | 偵測到兩個以上螢幕用 `dual`，一個用 `single`；偵測失敗用 `none` |
| `none` | 不指定位置與大小，由視窗管理員決定 |

座標由 [`window_layout.py`](../src/ofdm_message_link/window_layout.py) 計算。

### Log 的上限與清理

Demo 可以連續跑好幾個小時，所以每一種 log 都有上限，到了上限就丟掉最舊的內容：

| Log | 上限 | 調整方式 |
|---|---|---|
| GUI `Log` tab | 最後 2000 行 | app 的 `--log-lines N` |
| `rx_stats.jsonl` | 16 MiB ＋ 1 份舊檔 | 腳本 `--stats-max-mb`；app 的 `--stats-log-max-mb` |
| `rx.log`、`tx.log`、`ffplay.log`、`ffmpeg.log` | 各 4 MiB ＋ 1 份舊檔 | 腳本 `--log-max-mb` |
| Run 目錄 | 最新 5 個 | 腳本 `--keep-runs N` |

想保留完整紀錄就用 `--out` 指定目錄（不會被清理），並加 `--stats-max-mb 0 --log-max-mb 0`。

### 影像 demo 的排錯

| 症狀 | 原因 | 處理 |
|---|---|---|
| ffplay 視窗全黑，TX `Goodput (2 s)` 為 0 | ffmpeg 被暫停（狀態 `T`）或沒啟動 | `ps -o stat -C ffmpeg`；加 `-nostdin` 重跑 |
| 畫面破圖，RX 燈號變黃或紅 | SNR 不夠或速率太高 | 看燈號原因後的 `try MCS N` 建議；改 `--mcs-index 0 --video-kbps 750` |
| TX `Queue` 一直增加、燈號黃 `BUSY` | 影片 bitrate 超過 link 容量 | 降低 `--video-kbps` |
| 腳本說 `an earlier demo is still running` | 上一次的 process 沒清乾淨 | 見下方 |
| 終端機停在 `Video starts once both radios are on` | 還有一個視窗沒按 Start radio，或啟動失敗 | 看該視窗 Radio panel 下方的紅字 |

清掉殘留的 process（依這個順序，先停資料來源，最後停播放）：

```bash
pgrep -af "ofdm_message_link|ffmpeg|ffplay"                      # 先看有什麼
pkill -TERM -x ffmpeg
pkill -TERM -f '^python -m ofdm_message_link[.]tx_app'
pkill -TERM -f '^python -m ofdm_message_link[.]rx_app'
pkill -TERM -x ffplay
```

GUI 主程式結束後，偶爾會留下 `multiprocessing.forkserver` 子 process，它們會佔住 radio。
用 `pgrep -af forkserver` 檢查，確認命令列含 `ofdm_message_link` 後再 `kill -TERM <pid>`。

下一篇：[用真的 SDR 發射](04-ota-hardware.md)。

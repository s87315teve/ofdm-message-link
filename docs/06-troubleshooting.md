# 6. 收不到東西？排錯清單

## 6.1 先分清楚是哪一層的問題

看 RX 視窗的 `Signal` tab：

| 你看到的 | 代表 | 往哪裡查 |
|---|---|---|
| Spectrum 和 constellation **都沒有資料** | 根本沒有 samples 進來 | Sample transport：RX 有沒有先開、port 對不對、radio 有沒有按 Start |
| Spectrum 在動，constellation 顯示 `no decoded burst yet` | Samples 有進來，但沒有 burst 通過 synchronization 與 CRC | 6.3 的表：gain、antenna port、sample rate |
| Constellation 有點，但 `Loss (10 s)` 大於 0 | Link 通了，但 SNR 不夠或速率太高 | 換比較穩的 MCS、調 gain、降低送出速率 |

## 6.2 模擬（`--transport udp`）

| 症狀 | 多半是 | 怎麼辦 |
|---|---|---|
| `python -m ofdm_message_link.rx_app` 說 `No module named ofdm_message_link` | 沒有啟用環境，或沒有安裝 | `conda activate ofdm-message-link`，然後 `pip install -e .` |
| `configuration file not found: configs/default.yaml` | 不是從 repository root 執行 | `cd` 到 clone 下來的資料夾再執行 |
| `ImportError` 提到 `_turbo_native` | C++ decoder 沒有編譯 | 在環境裡重新 `pip install -e .`，看編譯有沒有錯誤訊息 |
| 訊息時有時無，或完全沒到新開的 RX | 上一個 RX 還在跑。兩個 process 可以同時 bind 同一個 UDP port，不會報錯 | `pgrep -af ofdm_message_link` 找出舊的 process 並關掉 |
| TX 送了但 RX 沒反應 | RX 比 TX 晚開，或兩邊的 `--udp-port` 不同 | 先開 RX；兩邊用同一個 port |
| 視窗開不起來，訊息提到 `xcb` 或 display | 沒有圖形桌面（例如純 SSH） | 在有桌面的機器上執行，或用 `ssh -X` |
| `Warning: Ignoring XDG_SESSION_TYPE=wayland` | Qt 在 Wayland 上改用 XWayland | 無害，可忽略 |
| 打中文或 emoji、或收到含這些字的訊息時，視窗直接消失（`Segmentation fault`） | 家目錄的字型快取 `~/.cache/fontconfig` 裡有壞掉的項目，見 [6.7](#67-打中文視窗就閃退) | 更新到最新版（app 已經會避開）；自己寫的 Qt 程式照 6.7 處理 |

## 6.3 真的 radio

照順序檢查：

| # | 症狀 | 多半是 | 怎麼辦 |
|---:|---|---|---|
| 1 | TX 的 log 一直增加，RX 什麼都沒有；TX 視窗底部有橘色警告 | **TX gain 在最低值** | `Hardware` tab 調高 `Gain (dB)`，執行中立即生效；或啟動時帶 `--gain` |
| 2 | RX spectrum 有東西但解不出來，`rx level` 的 `spread` 很小 | **RX antenna 接錯 port**（例如線接在 `TX/RX` 卻選了 `RX2`） | 換 antenna port 比調高 gain 有用得多 |
| 3 | 啟動時錯誤訊息寫出兩個不同的 frequency | Frequency 超出裝置範圍（UHD 會默默改到邊界） | 改用兩台都支援的 frequency，見 [N210 注意事項](04-ota-hardware.md#usrp-n210-注意事項) |
| 4 | 完全對不上，連 preamble 都偵測不到 | **兩端 sample rate 不同** | 兩端用同一個 `--overlay`，或在兩個 Radio panel 選同一個 sample rate |
| 5 | `No UHD Devices Found`，或訊息提到 `Could not find path for image` | 沒有下載 FPGA images | `uhd_images_downloader -t b2xx` |
| 6 | Device 清單沒有 N210 | 網卡沒有同網段 IP | `sudo ip addr add 192.168.10.1/24 dev <你的網卡名稱>`，再按 Refresh |
| 7 | Probe 失敗、Radio panel 說裝置被佔用 | 另一個 process 已經開著這台 | 確認兩端 `--serial` 不同；清掉殘留 process（見 [3.3 節最後](03-your-own-app.md#影像-demo-的排錯)） |
| 8 | 高速率時大量掉包，但 SNR 看起來很高 | RX gain 太高、signal 太強，acquisition 跟不上 | RX 降到 15 dB 左右 |
| 9 | `Hardware` tab 的 overflow 一直增加 | USB bandwidth 或 CPU 不夠 | 兩台 USRP 分開 USB3 controller；維持 `--engine process`；關掉其他重負載程式 |
| 10 | Pluto：probe 失敗，訊息說 `is Pluto …, not …` | 兩台 Pluto 都在出廠位址 `192.168.2.1`，而且沒有 USB 存取權 | 裝 udev rule 後重新插拔，見 [ADALM-Pluto](04-ota-hardware.md#43-adalm-pluto) |
| 11 | Pluto：Device 清單是空的 | USB 沒列舉到（`lsusb` 看不到 `0456:b673`） | 重新插拔、換線或換 port；Pluto 開機約 20 秒 |
| 12 | Pluto：`rx_overflow_events` 一直增加、大量掉包 | RX gain 太高，或同一台 Pluto 同時收發 | RX 降到 15 dB 左右；收發各用一台 |

## 6.4 TX gain 為什麼預設是最低值

`Gain (dB)` 預設是裝置範圍的最低值（B210 是 0 dB），這是刻意的安全設計：啟動 radio 不應該用
一個任意的功率去驅動 power amplifier。代價是第一次跑會**看起來像壞掉，其實只是很安靜**。
這個狀態下送出訊息時，發送端視窗底部會出現：

```
TX gain is 0 dB, the minimum for this device. Bursts are going out but the
receiver will almost certainly hear nothing. Raise Gain on the Hardware tab
(range 0-89.75 dB); it takes effect immediately.
```

`Gain (dB)` 在 radio 執行中仍可編輯，按上下箭頭、按 Enter 或離開欄位時立即套用，不必 Stop radio，
另一端也不用跟著改。

## 6.5 用 RX level 判斷 antenna port

一個實際發生過的例子：第一次實測完全解不出東西，原因不是功率不夠，而是 **RX antenna 接在
`TX/RX`，Radio panel 卻選了 `RX2`**。同一個 front end 的 `RX2` 仍然收得到漏過來的 signal：
足以在 spectrum 上看起來像有東西，又不足以 decode。

接收端 `Signal` tab 因此提供一行 level 讀數：

```
rx level now -64.7 dBFS   quietest -65.2 dBFS   loudest -43.9 dBFS   spread 21.3 dB  (signal present)
```

**看 `spread`。** 沒接東西的 port 會一直停在自己的 noise floor，spread 接近 0；有 signal 的 port
會隨發射開關明顯跳動。

## 6.6 還是找不到原因

1. 回到模擬（`--transport udp`）。模擬會通、實機不通，問題就在 radio 這一段。
2. 改用 cable 加 attenuator。Cable 會通、antenna 不通，問題在 antenna、距離或干擾。
3. 退回 MCS 0。它需要的 SNR 最低。
4. RX 加上 `--stats-log rx.jsonl`，從每一行的 `decoder` 欄位看 burst 卡在哪一步（`Decode` tab 顯示
   的是同一組計數）：

   | 計數 | 沒有增加時代表 |
   |---|---|
   | `detected_burst_candidates` | 連 preamble 都沒找到：synchronization 的問題（gain、sample rate、antenna port） |
   | `header_decode_success` | 找到 preamble 但 header 解不出來：SNR 太低或頻率偏太多 |
   | `valid_decoded_bursts` | Header 過了但 payload 失敗（`crc_failures` 在增加）：payload 的 SNR 不夠，換比較穩的 MCS |

## 6.7 打中文視窗就閃退

症狀：英文訊息正常，但只要畫面要顯示一個中文字或 emoji，TX 與 RX 兩個視窗就一起消失，終端機
只留下 `Segmentation fault (core dumped)`。`journalctl -k | grep segfault` 會看到它死在
`libfontconfig.so`。

這不是編碼問題。訊息從頭到尾都是 UTF-8，`udp_recv` 印出來的中文是對的。出事的是「把字畫出來」：
預設字型沒有中文，Qt 請 fontconfig 找一個有的，而 fontconfig 讀到一筆壞掉的快取就 crash 了。

`~/.cache/fontconfig` 是整台機器上所有程式共用的，每個程式用的 fontconfig 版本不一定相同。某個
程式看不懂某種字型（我們遇到的是 `/usr/share/fonts/woff/` 底下的 WOFF 字型，由
`fonts-ebgaramond-extra` 這類套件安裝）時，會寫下一筆沒有字元表的項目，conda 環境裡的 fontconfig
讀到它就會出事。可以這樣確認（要在 `conda activate ofdm-message-link` 之後執行）：

```bash
fc-cat -v ~/.cache/fontconfig/*.cache-* 2>/dev/null | grep -B3 '":fontwrapper=WOFF"' | head
```

有輸出就是中了：正常的項目後面會有一長串 `family=...:charset=...`。

**這個專案的兩個視窗已經會避開**：`qt_runtime.py` 的 `private_font_cache()` 在 Qt 載入字型的那
一刻，讓 fontconfig 不去讀那個共用的快取。字型還是用系統的，只是快取改用 conda 環境自己的那一份。

你自己寫的 PyQt 程式遇到同樣的問題時，兩個解法擇一：

```bash
# 只影響這一次執行
XDG_CACHE_HOME=/tmp/my-cache python my_app.py

# 或把壞掉的那個檔改名（上面指令輸出裡的 "Cache:" 那一行就是檔名）；它之後可能被重新寫壞
mv ~/.cache/fontconfig/<那個檔> ~/.cache/fontconfig/<那個檔>.bad
```

回到 [README](../README.md)。

# 4. 用真的 SDR 發射

到目前為止 samples 都只在同一台電腦的 UDP 裡傳。這篇把 channel 換成真的 radio。
PHY、GUI 與量測完全不變，只有 sample transport 換掉。

## 4.1 發射前必讀

> ⚠️ **無線電頻譜是受管制的。** 接上 antenna 發射之前，**先向指導老師確認實驗室可以使用的頻段與
> 功率**。不確定的時候，用 cable 做，不要用 antenna。

請照這個順序做，不要跳步：

1. **先用模擬跑通**（[README 的五分鐘上手](../README.md#五分鐘上手不需要硬體)）。確認程式與環境沒問題。
2. **用 cable 加 attenuator 直連。** 用 SMA cable 把 TX 的 port 接到 RX 的 port，中間串一個
   **30 dB 以上的 attenuator**。這樣沒有任何訊號輻射出去，channel 也最乾淨，最適合第一次除錯。
   **不要沒接 attenuator 就把 TX 直接接到 RX**：TX 的輸出功率可能超過 RX 輸入端的最大額定值而
   損壞 radio。
3. **確認可以發射之後，才換成 antenna。** 用最低的 TX gain 開始，慢慢往上調到 RX 的 `Link` light
   變綠就停。兩支 antenna 放近一點，比把 gain 調高好。

程式裡對應的保護機制：

- **RF 發射預設是關閉的。** 發送端必須明確帶 `--enable-rf` 與一字不差的 `--acknowledgement`。
  少了任何一個，程式會在**視窗建立之前**就拒絕執行。一個沒有被授權發射的 process，不可能靠
  點按鈕變成有授權。接收端不發射，不需要這兩個參數。
- **TX gain 預設是裝置的最低值。** 提高功率是你的明確動作，不是按 Start 就發生的預設值。
- **按下 Start radio 之前不會有任何 RF 輸出。**

### 預設頻率

預設 profile 是 [`configs/profiles/ota_2p45ghz.yaml`](../configs/profiles/ota_2p45ghz.yaml)：
**2.45 GHz**、5 MS/s、QPSK Turbo 1/3。

- 2.45 GHz 在 2.4 GHz ISM 頻段內，B210／2901、N210（CBX daughterboard）與 ADALM-Pluto 都調得到。
- 這個頻段和 Wi-Fi、Bluetooth 共用，所以用 antenna 時會有干擾，constellation 不會像 cable 那麼乾淨。
- **這個預設值沒有經過 OTA 實測。** 實測過的條件是另外兩個選用的 profile（1.2 GHz 與 3.8 GHz），
  見[實測紀錄](08-measurements.md)。那兩個頻段都不是 ISM 頻段，只有在你確定可以使用時才用。

要換頻率或 sample rate，自己寫一個 profile（複製 `ota_2p45ghz.yaml` 來改），然後**兩端**都加
`--overlay 你的檔案.yaml`。也可以直接在 Radio panel 改 `Centre (MHz)`。

## 4.2 USRP（B210、NI USRP-2901、N210）

### 一次性設定

B210 與 2901 每次開啟時要從主機載入 FPGA image。在 conda 環境裡下載一次：

```bash
conda activate ofdm-message-link
uhd_images_downloader -t b2xx
uhd_find_devices            # 應該列出你的 USRP 與它的 serial
```

USB 的 USRP 需要 udev rule 才能讓一般使用者開啟。`uhd_find_devices` 找得到裝置但開啟時說
permission denied 的話，依 [UHD 手冊的說明](https://files.ettus.com/manual/page_transport.html#transport_usb_udev)安裝 `uhd-usrp.rules`。

### 啟動

**接收端**（先開）：

```bash
python -m ofdm_message_link.rx_app --transport uhd
```

**發送端**：

```bash
python -m ofdm_message_link.tx_app --transport uhd \
    --enable-rf \
    --acknowledgement 'I acknowledge that this process will transmit RF'
```

兩個視窗都會停在 `Hardware` tab 的 **Radio panel**：選裝置、antenna、gain 後按 **Start radio**。
想省一步可以用 `--serial <serial>` 預選某一台，或加 `--auto-start` 讓 probe 完成後直接啟動。

> 💡 **第一次跑常見的「看起來壞掉」：TX gain 預設是最低值。** Burst 有送出去但接收端幾乎聽不到。
> 到 `Hardware` tab 把 `Gain (dB)` 調高，執行中就會立即生效。

### Radio panel

| 欄位 | 來源 |
|---|---|
| **Device** | `uhd.find()` 列舉到的每一台，顯示名稱、型號與 serial。**Refresh** 重新列舉 |
| **Front end** | 選到裝置後實際開啟它讀回的 channels。B210／2901 是 `RF A (ch 0)` 與 `RF B (ch 1)`；N210 是 `Slot A (ch 0)` |
| **Antenna** | 該 channel 該方向實際回報的 port。RX 有 `TX/RX` 與 `RX2`，**TX 只有 `TX/RX`**（N210 front panel 上是 RF1／RF2） |
| **Gain (dB)** | 上下限直接取自裝置（B210 為 RX 0–76、TX 0–89.75；N210 + CBX 為 0–31.5）。**Radio 執行中仍可調整**，立即套用 |
| **Centre (MHz)** / **Sample rate** | Center frequency 與 sample rate；sample rate 限於 5／10／20 MS/s |
| **Start radio** | 按下去才會建立 UHD flowgraph。在此之前不會有任何 RF 輸出 |

設計上的幾個重點：

- **清單全部來自 live probe，不是寫死的表。** 只有「RF A / RF B」這種 front panel 上印的名稱放在
  `devices.py` 的 `DEVICE_FAMILIES` 表裡，因為 UHD API 不提供。未列出的裝置家族照樣能用，只是
  channel 顯示為 `Channel 0/1…` 並標示 `[untested family]`。
- **開啟裝置需要幾秒**（B2xx 要載入 FPGA image），所以列舉與 probe 都在背景 thread，期間 Start 是停用的。
- **同一台裝置不能同時被兩個 process 開啟。** 一邊已在串流時，另一邊 probe 它會失敗，panel 會說明原因。
- **Gain 以外的欄位執行中是鎖住的。** 要換裝置、antenna 或頻率，先 Stop radio。
- 未啟動 radio 時打的字**會排隊**，按下 Start 後一次送出，不會被丟掉。

### B210／2901 的 channel 編號

B210／2901 回報的 subdev spec 是 `A:A A:B`，依 channel 順序對應 front panel：

| UHD channel | subdev | Front panel |
|---:|---|---|
| 0 | `A:A` | **RF A** |
| 1 | `A:B` | **RF B** |

不要用 `get_rx_subdev_name()` 當標籤。它回報的是 AD9361 內部名稱（`FE-RX2`／`FE-RX1`），
順序相反，會把 port 標反。

### USRP N210 注意事項

| 項目 | 說明 |
|---|---|
| 連線 | Gigabit Ethernet。接 N210 的網卡必須有同網段 IPv4 位址，UHD 才找得到它：`sudo ip addr add 192.168.10.1/24 dev <你的網卡名稱>`（N210 出廠 IP 是 192.168.10.2；`ip -br link` 可以查網卡名稱；這是暫時性設定，重開機後要再做一次） |
| 選擇 | 跟 B2xx 一樣用 serial：`--serial <N210_SERIAL>`。Device 清單會顯示型號、serial 與 IP |
| Front end | 只有一個 daughterboard slot，顯示為 `Slot A (ch 0)` |
| Antenna | 顯示成 front panel 名稱：`TX/RX (RF1)`、`RX2 (RF2)`。**TX 只能用 RF1** |
| Frequency／gain | 由 daughterboard 決定，GUI 讀回實際範圍。例如 CBX 是 1.2–6 GHz、TX/RX gain 0–31.5 dB。超出範圍時 Start 前就會被拒絕 |
| Analog bandwidth | CBX 固定 40 MHz；實際 bandwidth 由 N210 的 digital filtering 決定 |

**Frequency 一定要確認有調到。** UHD 遇到超出範圍的 frequency 不會報錯，只會默默改成最近的邊界：
要 CBX 調到 915 MHz，實際上會停在 1180 MHz，而另一端真的在 915 MHz，結果就是什麼都收不到。
兩個 app 建好 UHD block 後會讀回實際 center frequency，偏差超過 1 kHz 就拒絕啟動，並在錯誤訊息裡
寫出兩個 frequency。

### 同一台電腦接兩台 USRP

可以，但：

- 5 MS/s complex64 單向約 40 MB/s。兩台 USB 裝置最好接在**不同的 USB3 controller** 上，
  避免共用 bandwidth 造成 overflow。
- 兩台必須用 `--serial` 明確指定，否則兩個 process 會搶同一台。
- 只有在**同一台電腦**時，接收端才會顯示 end-to-end latency。

## 4.3 ADALM-Pluto

Pluto 不是 UHD 裝置，所以用另一個 transport：`--transport pluto`。其餘完全相同：同一個
Radio panel、同一套 GUI、同一個 RF 授權閘門、同一份 PHY。

**① 一次性設定：讓一般使用者能開 Pluto 的 USB 裝置。** 這是 ADI 官方的 udev rule
（`53-adi-plutosdr-usb.rules`），裝完重新插拔 Pluto：

```bash
sudo tee /etc/udev/rules.d/53-adi-plutosdr-usb.rules >/dev/null <<'RULES'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b674", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", ENV{ID_MM_DEVICE_IGNORE}="1"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=0456
```

沒有這條 rule 時，程式會退回 Pluto 的 USB 網路位址（`ip:192.168.2.1`）。**單獨一台**這樣可以用；
**兩台都是出廠位址時會互撞**（兩台都是 `192.168.2.1`），Radio panel 會直接說 probe 到的 serial
不對，並提示裝這條 rule。

**② 啟動**（serial 在 Radio panel 的 Device 清單裡，也可以用 `lsusb -v -d 0456:b673` 查）：

```bash
# 接收端（先開）
python -m ofdm_message_link.rx_app \
    --transport pluto --serial <RX_PLUTO_SERIAL> --gain 15 --auto-start

# 發送端
python -m ofdm_message_link.tx_app \
    --transport pluto --serial <TX_PLUTO_SERIAL> --gain -15 --auto-start \
    --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
```

不加 `--auto-start` 就和 USRP 一樣停在 Radio panel，選好裝置與 gain 再按 **Start radio**。

**③ 和 USRP 不一樣的地方：**

| 項目 | Pluto |
|---|---|
| Front end／Antenna | 一個 channel（`RF (ch 0)`）；port 就是外殼上的 `RX` 與 `TX` 兩個 SMA，每個方向只有一個選項 |
| **TX gain 是 attenuator** | 範圍 **−89.75 到 0 dB**，0 dB 是最大輸出（幾 dBm）。預設 −89.75 dB，等於沒有輸出 |
| RX gain | Manual gain，範圍由裝置回報（隨 frequency 不同，約 −3 到 71 dB） |
| **Sample rate 只有 5 MS/s** | USB 2.0 的上限：要求 10 MS/s 時實收只有 5.5 MS/s。Panel 選 10／20 MS/s 會在 Start 前被拒絕 |
| 沒有 timed TX | Pluto 沒有 device clock，burst 一送到 FPGA 就發射；所以沒有 late／underflow 計數，`Hardware` tab 顯示 `UHD faults: n/a for this transport` |
| RX 掉 sample | FPGA 只給一個 overflow flag。`rx_overflow_events` 是「輪詢到 flag 的次數」，是下限，也不知道每次掉多少 sample |
| Frequency 範圍 | 由裝置回報。原廠 AD9363 是 325–3800 MHz；調不到要求的 frequency 時拒絕啟動 |
| 停止 radio | TX attenuator 會被設回 −89.75 dB。Pluto 的 TX chain 只要板子有電就開著，所以停止時主動降到最小 |
| 一台一個 process | 走 USB 時，同一台 Pluto 同時只能被一個 process 開啟；收發請各用一台 |
| 調頻時的 carrier | TX 第一次調到新 frequency 時，量到約 33 ms、接近滿輸出的未調變 carrier，**不受 gain 設定控制**（研判是 AD936x 的 TX calibration）。**這是用 cable 加 attenuator 的另一個理由** |

**④ 建議起點：TX −15 dB／RX 15 dB**（兩台放在同一張桌上、各接原廠 antenna 的實測值）。
RX gain 太高（30 dB）加上較強的 signal，接收端的 acquisition 會跟不上而掉包；先降 RX gain 再調 TX。

Pluto 和 USRP 可以各在一端（每個 process 自己選 `--transport`），但**這個組合還沒有實測過**。

## 4.4 為什麼 burst 要做 peak normalization

OFDM burst 離開 encoder 時 PAPR（peak-to-average power ratio）約 15 dB，peak 遠大於 1.0。
Radio 的 DAC 會把超出範圍的部分 clip 掉，所以每個 burst 發射前都依**自己的 peak**縮放到
`--peak-amplitude`（預設 0.7）。用 peak 而不是 power 來縮放，保證不論 payload 內容為何都不會 clipping。

下一篇：[新增一種 SDR](05-add-a-new-sdr.md)。遇到問題看[排錯清單](06-troubleshooting.md)。

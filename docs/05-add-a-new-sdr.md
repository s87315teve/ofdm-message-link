# 5. 新增一種 SDR

這個專案現在支援 UHD（USRP）與 ADALM-Pluto。想接別的 SDR（例如 HackRF、LimeSDR、bladeRF 或
RTL-SDR）時，**PHY 與 GUI 都不用動**，只要補一個 sample transport。

ADALM-Pluto 就是這樣加進來的：一個新檔案 [`pluto.py`](../src/ofdm_message_link/pluto.py)，加上
`devices.py` 與 `options.py` 裡的幾行。這篇用它當範例。

## 5.1 介面在哪裡

PHY 產生的是一段 `complex64` 的 NumPy array（一個 burst），需要的也只是一串 `complex64` 的 samples。
中間那一層就是 sample transport，定義在
[`transport.py`](../src/ofdm_message_link/transport.py) 開頭：

```python
class SampleSink(Protocol):
    """Somewhere to put one burst's samples."""
    def start(self) -> None: ...
    def send(self, samples: NDArray[np.complex64]) -> bool: ...
    def stop(self) -> None: ...
    def snapshot(self) -> dict[str, object]: ...


class SampleSource(Protocol):
    """Somewhere to get a continuous stream of samples from."""
    def start(self) -> None: ...
    def recv(self, timeout: float) -> NDArray[np.complex64] | None: ...
    def stop(self) -> None: ...
    def snapshot(self) -> dict[str, object]: ...
```

你的新裝置要提供一個 sink（發射）和一個 source（接收），各自滿足上面四個 method，再加上：

| 成員 | 必要 | 說明 |
|---|---|---|
| `description`（property） | 是 | 一行文字，顯示在 `Hardware` tab 與錯誤訊息裡 |
| `set_gain(gain_db) -> float` | 選用 | 有的話，radio 執行中就能在 GUI 調 gain。回傳裝置實際套用的值 |

每個 method 的約定：

| Method | 約定 |
|---|---|
| `start()` | 開啟裝置、調頻、設 gain。失敗時 raise `TransportError`，錯誤訊息要寫出原因。**在這之前不可以有任何 RF 輸出** |
| `sink.send(samples)` | 送出**一整個 burst**。先呼叫 `scale_to_peak(samples, peak_amplitude)` 避免 clipping。成功回傳 `True`。這個呼叫可以 block 到裝置收下為止（這就是 back-pressure：下游忙不過來時，讓上游等待，而不是丟掉資料） |
| `source.recv(timeout)` | 回傳下一段連續的 samples；`timeout` 秒內沒有就回傳 `None`。每次回傳的長度不限，但**不可以跳過或重排 samples**。遺失 samples 時要在 `snapshot()` 裡計數 |
| `stop()` | 關閉裝置，並讓 TX 回到不發射的狀態。要能重複呼叫 |
| `snapshot()` | 回傳一個 dict，內容會原樣列在 `Hardware` tab。放你想觀察的計數，例如送出的 burst 數、overflow 次數、裝置讀回的 frequency 與 gain |

Samples 的單位：`complex64`，滿刻度是 ±1.0。你的 sink 要負責轉成裝置的格式（Pluto 是 12-bit 的
`int16`，見 `pluto.py` 的 `_TX_FULL_SCALE`）；source 要轉回來。

**效能上的規定：不要在 Python 裡一個 sample 一個 sample 處理。** 5 MS/s 是每秒五百萬個 sample。
一律用整個 buffer 的 NumPy 運算，或交給裝置的 library。

## 5.2 要改的地方

假設新裝置叫 `mysdr`。

### ① 新檔案 `src/ofdm_message_link/mysdr.py`

照 `pluto.py` 的結構，提供五樣東西：

| 名稱 | 做什麼 | Pluto 的版本 |
|---|---|---|
| `discover()` | 列出接著的裝置，**不開啟**它們。回傳一組 `DiscoveredDevice`（serial、name、driver、address） | 掃 USB sysfs 找 vendor／product ID |
| `probe(device)` | 開啟一台，讀回它實際的 gain 範圍、frequency 範圍、antenna port。回傳 `DeviceCapabilities` | 透過 libiio 讀 `hardwaregain_available`、`frequency_available` |
| `settings_for(selection)` | 把 Radio panel 的選擇（`RadioSelection`）轉成你的設定物件 | `PlutoSettings` |
| `MySdrSampleSink` | 上一節的 sink | `PlutoSampleSink` |
| `MySdrSampleSource` | 上一節的 source | `PlutoSampleSource` |

`DiscoveredDevice`、`DeviceCapabilities`、`ChannelCapability`、`RadioSelection` 都定義在
[`devices.py`](../src/ofdm_message_link/devices.py)。Radio panel 只認得這幾個型別，所以只要你的
`discover()` 與 `probe()` 回傳它們，panel 就會自動顯示你的裝置、它的 gain 範圍與 antenna 選項。

**範圍一律從裝置讀回，不要寫死。** 這樣 panel 才會在按 Start 之前就擋掉裝置做不到的設定。
如果裝置的 host 介面有速率上限（Pluto 的 USB 2.0 只撐得住 5 MS/s），填在
`DeviceCapabilities.max_sample_rate`。

### ② `devices.py`：登記裝置家族並分派

在 `DEVICE_FAMILIES` 加一筆，提供 front panel 上印的名稱：

```python
"mysdr": DeviceFamily(
    driver="mysdr",
    description="My SDR",
    channel_labels=("RF",),
),
```

然後在 `discover()` 與 `probe()` 裡各加一個分支，照 `pluto` 的寫法轉交給你的 module：

```python
if backend == "mysdr":
    from . import mysdr
    return mysdr.discover()
```

（`import` 放在分支裡，是為了沒有裝這個裝置 library 的人也能正常使用其他 transport。）

### ③ `options.py`：加進命令列並建立 transport

```python
RADIO_TRANSPORTS = ("uhd", "pluto", "mysdr")
```

`RADIO_TRANSPORTS` 裡的名稱會自動出現在 `--transport` 的選項中、得到 Radio panel，發送端也會自動
套用 RF 授權閘門。接著在 `build_sink()` 與 `build_source()` 裡各加一個分支：

```python
if options.transport == "mysdr":
    from . import mysdr
    return mysdr.MySdrSampleSink(
        mysdr.settings_for(_selected(selection)), token, peak_amplitude=peak
    )
```

### ④ RF 授權閘門：**一定要保留**

`build_sink()` 會先呼叫 `require_rf_capability(args)` 拿到一個 token，再把它交給你的 sink。
你的 sink 必須在 `start()` 裡檢查它，沒有有效的 token 就不開啟裝置：

```python
from ofdm_link.radio.uhd import RFEnableToken

def start(self) -> None:
    if not isinstance(self._rf_enable, RFEnableToken):
        raise TransportError("transmitting needs --enable-rf and its acknowledgement")
    ...
```

另外兩個安全上的慣例也請照做：**TX gain 預設是裝置的最低值**（`devices.default_selection()` 已經
這樣處理，只要你的 `probe()` 回報正確的 gain 範圍）；**`stop()` 要把 TX 降回最小輸出**。

### ⑤ 測試

Pluto 的測試在 [`tests/app/test_message_link_pluto.py`](../tests/app/test_message_link_pluto.py)，
全部不需要硬體：用假的 USB sysfs 目錄測 `discover()`，用一個假的 libiio context 物件測 `probe()`、
sink 與 source。做法是讓你的 class 接受一個可替換的「開啟裝置」函式（Pluto 的 `open_context`
參數），測試時傳入假的版本。

至少測這幾件事：

- `discover()` 找得到你的裝置，並忽略其他 USB 裝置。
- `probe()` 回報的範圍和假裝置給的一致。
- **沒有 RF token 時，sink 不會開啟任何裝置。**
- `send()` 送出的 sample 數等於 burst 的長度，數值換算正確、沒有 clipping。
- `stop()` 之後 TX gain 回到最低值。

```bash
python -m pytest tests/app -q
```

## 5.3 加完之後怎麼驗證

1. `python -m pytest -q` 全過。
2. 接上裝置，`python -m ofdm_message_link.rx_app --transport mysdr`，確認 Radio panel 列得出裝置，
   範圍合理。只開 RX 不會發射，可以放心試。
3. **用 cable 加 attenuator** 連接 TX 與 RX（見[發射前必讀](04-ota-hardware.md#41-發射前必讀)），
   啟動 TX，慢慢調高 gain 到 RX 的 `Link` light 變綠。
4. 用 `Send` tab 的 Loss test 送 1000 則，確認 loss 接近 0，`Hardware` tab 的 overflow 計數沒有增加。

## 5.4 幾個會遇到的問題

這些是加 Pluto 時實際遇到的，換別的裝置多半也會碰到類似的：

- **Burst 的長度不固定。** 每個 MCS、每則 message 的 burst 長度都不同。如果裝置的 API 只能送固定
  大小的 buffer，每個 burst 就會被補零到那個大小，佔用的 airtime 變長、throughput 掉很多。Pluto
  直接用 libiio 而不用 gr-iio 的 block 就是這個原因。
- **裝置沒有 device clock。** USRP 可以指定「在某個時間點發射」，所以能偵測 late 與 underflow。
  沒有這個功能的裝置就沒有這些計數，`snapshot()` 不要放 `uhd_faults`，GUI 會顯示 `n/a`。
- **Frequency 要讀回來確認。** 很多 driver 遇到超出範圍的 frequency 不會報錯，只會默默改成最近的
  邊界。`transport.require_tuned()` 示範了怎麼檢查。
- **Host 介面的速率上限。** USB 2.0 的裝置通常撐不住 10 MS/s。實際量一次「要求的 sample rate」
  和「真的收到的 sample rate」，再決定 `max_sample_rate`。
- **RX gain 不是越高越好。** Signal 太強時 burst 偵測會變慢而遺失 burst。先把 RX gain 放在中間偏低，
  再調 TX。

回到 [README](../README.md)。

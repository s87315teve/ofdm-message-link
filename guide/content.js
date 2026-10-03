// Text for the guide page, in Traditional Chinese with the ASD-STE100 habits kept:
// one fact per sentence, short sentences, active voice, the same word for the
// same thing.  Technical terms stay in English, as they are in the code.
// A value can be a string or {sim, uhd, pluto} when the sample transport changes it.
window.GUIDE_CONTENT = (function () {
  "use strict";

  var MODULES = [
    {
      id: "ext-in", cls: "ext", area: "ext-in", arrow: "right", row: "來源",
      name: "你的程式", line: "把 bytes 送到 UDP port",
      sum: "任何能寫 UDP socket 的程式，都能當資料來源。",
      blocks: [{
        name: "Producer",
        does: ["程式把 bytes 送到 UDP port 52001。", "程式不需要知道 MCS、FEC 或 radio。"],
        input: "文字、JSON、檔案或 video", output: "UDP datagrams",
        code: ["<code>udp_send.py</code>（最小範例）", "<code>ffmpeg</code>（video）"],
        opts: ["<code>--ingress-port</code>（預設 52001）"],
        gui: "TX 的 <code>Log</code> tab 列出每一則 message。"
      }]
    },
    {
      id: "tx-app", cls: "tx", area: "tx-app", arrow: "right", row: "TX",
      name: "Ingress", line: "GUI input box 與 UDP port 52001",
      sum: "TX 程式從兩個來源收 message。兩個來源走同一條路徑。",
      blocks: [{
        name: "Ingress",
        does: ["你在 input box 打字，然後按 Enter。", "其他程式也能把 bytes 送到 UDP ingress port。",
          "Radio 未啟動時，message 留在 queue。Radio 啟動後，TX 送出 queue 裡的全部 message。"],
        input: "打字的文字或 UDP bytes", output: "一則 message（bytes）",
        code: ["<code>tx_app.py</code>：<code>TransmitWindow</code>、<code>IngressListener</code>、<code>TransmitWorker</code>"],
        opts: ["<code>--ingress-port</code>（預設 52001）"],
        gui: "TX 的 <code>Send</code> tab。<code>Queue</code> card 顯示 queue 裡的 message 數量。"
      }]
    },
    {
      id: "tx-link", cls: "tx", area: "tx-link", arrow: "right", row: "TX",
      name: "Link", line: "Fragment、sequence number、CRC",
      sum: "Link layer 切分 message，並加上 RX 組回與檢查所需的資料。",
      blocks: [
        {
          name: "Datagram",
          does: ["這個 block 把 message 切成 fragments。每個 fragment 最多 968 bytes。", "它在每個 fragment 前面加 28-byte header。",
            "Header 內有 message ID、fragment index、fragment count 與 TX timestamp。"],
          input: "一則 message", output: "Datagrams（每個最多 996 bytes）",
          code: ["<code>datagram.py</code>：<code>fragment()</code>、<code>Datagram.encode()</code>"],
          opts: ["<code>--frame-payload-bytes</code>（預設 996）"],
          gui: "—"
        },
        {
          name: "PHY frame",
          does: ["這個 block 加上 7-byte frame header 與 CRC-32。", "Header 內有 16-bit sequence number。",
            "RX 用 sequence number 計算遺失的 burst 數量。RX 用 CRC 找出 decode 錯誤。"],
          input: "一個 datagram", output: "一個 PHY frame（最多 1007 bytes）",
          code: ["<code>link.py</code>：<code>MessageTransmitter.encode_message()</code>", "<code>ofdm_link.phy.codec</code>：<code>Frame</code>"],
          opts: ["—"], gui: "—"
        }
      ]
    },
    {
      id: "tx-phy", cls: "tx", area: "tx-phy", arrow: "right", row: "TX",
      name: "PHY", line: "Bits 變成 OFDM burst",
      sum: "PHY 把 frame bits 變成一個 burst 的 complex samples。",
      blocks: [
        {
          name: "FEC encoder",
          does: ["這個 block 加入 redundant bits。", "RX 用這些 bits 修正 noise 造成的錯誤。",
            "預設是 Turbo，rate 1/3。MCS 3 與 MCS 7 沒有 FEC，只有 CRC。"],
          input: "Frame bits", output: "Coded bits",
          code: ["<code>ofdm_link.phy.codec</code>（由 wire version 選擇 Turbo、convolutional 或 uncoded）"],
          opts: ["由 MCS 決定"],
          gui: "你選擇 uncoded MCS 時，TX 的 <code>Send</code> tab 顯示警告。"
        },
        {
          name: "Symbol mapper",
          does: ["這個 block 把 bits 變成 constellation points。", "QPSK 的每個 symbol 帶 2 bits。16QAM 的每個 symbol 帶 4 bits。",
            "16QAM 比較快，但是需要比較高的 SNR。"],
          input: "Coded bits", output: "Complex symbols",
          code: ["<code>ofdm_link.phy.codec</code>：<code>map_symbols</code>"],
          opts: ["由 MCS 決定"],
          gui: "RX 的 constellation 顯示 4 個或 16 個黃色 reference points。"
        },
        {
          name: "OFDM modulator",
          does: ["這個 block 把 48 個 data symbols 與 4 個 pilots 放進 64-point IFFT。", "然後它加上 16-sample cyclic prefix。",
            "一個 OFDM symbol 有 80 samples。"],
          input: "Complex symbols", output: "Time-domain OFDM symbols",
          code: ["<code>ofdm_link.phy.ofdm</code>：<code>modulate_ofdm</code>"],
          opts: ["<code>fft_len: 64</code>、<code>cyclic_prefix_len: 16</code>（<code>configs/default.yaml</code>）"],
          gui: "—"
        },
        {
          name: "Burst assembly",
          does: ["這個 block 依序接上四個部分：preamble、training symbol、3 個 header symbols、payload。",
            "Header symbols 固定使用 QPSK。Header 內有 MCS、wire version 與 length。",
            "Link 在 burst 的前面與後面各加 256 個 zero samples。"],
          input: "OFDM symbols", output: "一個 burst",
          code: ["<code>ofdm_link.phy.burst</code>：<code>encode_burst</code>", "<code>link.py</code>：加上 guard samples"],
          opts: ["—"],
          gui: "TX 的 <code>Airtime (2 s)</code> card 顯示 burst 佔用的時間比例。"
        }
      ]
    },
    {
      id: "tx-tr", cls: "tx", area: "tx-tr", arrow: "right", row: "TX",
      name: "Sample transport",
      line: { sim: "UDP sink，127.0.0.1:52101", uhd: "UHD sink，timed burst", pluto: "Pluto sink，經過 libiio" },
      sum: "Simulation 與硬體之間，TX 只有這個模組不同。",
      blocks: [
        {
          name: "Peak normalize",
          does: ["OFDM burst 的 PAPR 大約是 15 dB。Peak 常常大於 1.0。", "Radio 會 clip 大於 1.0 的 samples。",
            "所以這個 block 把每個 burst 的 peak 縮放到 0.7。"],
          input: "一個 burst", output: "Peak 為 0.7 的 burst",
          code: ["<code>transport.py</code>：<code>scale_to_peak</code>"],
          opts: ["<code>--peak-amplitude</code>（預設 0.7）"], gui: "—"
        },
        {
          name: "Sample sink",
          does: {
            sim: ["這個 block 把每個 burst 切成 UDP datagrams。", "它把 datagrams 送到同一台電腦的 port。"],
            uhd: ["這個 block 經過 GNU Radio 的 UHD sink 發射每個 burst。", "每個 burst 帶 <code>tx_time</code>。<code>tx_time</code> 比 device time 晚 50 ms。",
              "它在每個 burst 前面加 250 µs 的 zero samples。這段時間讓 front end 穩定。",
              "沒有 <code>--enable-rf</code> 與完整的 acknowledgement 時，這個 block 不會啟動。"],
            pluto: ["這個 block 經過 libiio 把每個 burst 送到 Pluto。", "Pluto 沒有 device clock。Burst 一到 FPGA 就發射。",
              "沒有 <code>--enable-rf</code> 與完整的 acknowledgement 時，這個 block 不會啟動。"]
          },
          input: "Complex samples", output: { sim: "UDP datagrams", uhd: "RF signal", pluto: "RF signal" },
          code: {
            sim: ["<code>transport.py</code>：<code>UdpSampleSink</code>"],
            uhd: ["<code>transport.py</code>：<code>UhdSampleSink</code>、<code>UhdSinkLimits</code>", "<code>options.py</code>：<code>require_rf_capability</code>"],
            pluto: ["<code>pluto.py</code>：<code>PlutoSampleSink</code>", "<code>options.py</code>：<code>require_rf_capability</code>"]
          },
          opts: {
            sim: ["<code>--udp-host</code>、<code>--udp-port</code>（預設 127.0.0.1:52101）"],
            uhd: ["<code>--serial</code>、<code>--channel</code>、<code>--antenna</code>、<code>--gain</code>", "<code>--enable-rf</code>、<code>--acknowledgement</code>"],
            pluto: ["<code>--serial</code>、<code>--gain</code>（−89.75 dB 到 0 dB）", "<code>--enable-rf</code>、<code>--acknowledgement</code>"]
          },
          gui: "TX 的 <code>Hardware</code> tab。<code>TX</code> light 顯示 <code>RF ON</code> 或 <code>SIMULATED</code>。"
        }
      ]
    },
    {
      id: "channel", cls: "ch", area: "channel", row: "",
      name: "Channel",
      line: { sim: "Localhost UDP + AWGN", uhd: "Cable 或 over the air，2.45 GHz，5 MS/s", pluto: "Cable 或 over the air，2.45 GHz，5 MS/s" },
      sum: "Channel 把 samples 從 TX 帶到 RX。",
      blocks: [{
        name: "Channel",
        does: {
          sim: ["Samples 經過同一台電腦的 UDP。", "RX 可以用 <code>--snr-db</code> 加上 AWGN。",
            "這個 channel 只加 noise。它沒有 fading、CFO 或 sample clock offset。"],
          uhd: ["Signal 經過 cable 或空中傳送。", "預設 center frequency 是 2.45 GHz。預設 sample rate 是 5 MS/s。",
            "TX 與 RX 的 sample rate 必須相同。MCS 可以不同。"],
          pluto: ["Signal 經過 cable 或空中傳送。", "預設 center frequency 是 2.45 GHz。",
            "Pluto 經過 USB 2.0 只能使用 5 MS/s。"]
        },
        input: "TX 的 samples", output: "含 noise 的 samples",
        code: { sim: ["<code>transport.py</code>：<code>UdpSampleSource</code>（加上 AWGN）"], uhd: ["<code>configs/profiles/ota_2p45ghz.yaml</code>"], pluto: ["<code>configs/profiles/ota_2p45ghz.yaml</code>"] },
        opts: { sim: ["<code>--snr-db</code>（只在 RX 設定）"], uhd: ["<code>--overlay</code>：選擇 center frequency 與 sample rate"], pluto: ["<code>--overlay</code>：選擇 center frequency"] },
        gui: "RX 的 spectrum 與 <code>SNR (2 s)</code> card。"
      }]
    },
    {
      id: "rx-tr", cls: "rx", area: "rx-tr", arrow: "left", row: "RX",
      name: "Sample transport",
      line: { sim: "UDP source，連續 stream", uhd: "UHD source，連續 stream", pluto: "Pluto source，經過 libiio" },
      sum: "RX 不知道 burst 什麼時候來。所以 RX 一直接收連續的 sample stream。",
      blocks: [{
        name: "Sample source",
        does: {
          sim: ["這個 block 接收 UDP datagrams，然後組成連續的 stream。", "在 burst 之間，它補上 idle samples。有 <code>--snr-db</code> 時是 AWGN，沒有時是 zeros。",
            "所以 decoder 連續搜尋 preamble。這和硬體的情況相同。"],
          uhd: ["這個 block 連續接收 USRP 的 samples。", "它計算 overflow 的次數。"],
          pluto: ["這個 block 連續接收 Pluto 的 samples。", "Pluto 只提供一個 overflow flag。所以 overflow 次數是下限。"]
        },
        input: { sim: "UDP datagrams", uhd: "RF signal", pluto: "RF signal" }, output: "連續的 sample stream",
        code: { sim: ["<code>transport.py</code>：<code>UdpSampleSource</code>"], uhd: ["<code>transport.py</code>：<code>UhdSampleSource</code>"], pluto: ["<code>pluto.py</code>：<code>PlutoSampleSource</code>"] },
        opts: { sim: ["<code>--snr-db</code>"], uhd: ["<code>--serial</code>、<code>--channel</code>、<code>--antenna</code>、<code>--gain</code>", "<code>--rx-recv-frames</code>"], pluto: ["<code>--serial</code>、<code>--gain</code>"] },
        gui: "RX 的 <code>Signal</code> tab：<code>rx level</code>。RX 的 <code>Hardware</code> tab：overflow 次數。"
      }]
    },
    {
      id: "rx-phy", cls: "rx", area: "rx-phy", arrow: "left", row: "RX",
      name: "PHY", line: "Samples 變回 frame bits",
      sum: "PHY 在 stream 裡找到每個 burst，然後 decode。Burst header 告訴 RX 要用哪一種 MCS。",
      blocks: [
        {
          name: "Acquisition",
          does: ["這個 block 用 Schmidl–Cox metric 找 preamble。", "Preamble 在 time domain 有兩個相同的 halves。",
            "這個 block 同時估計 fractional CFO 與 gain。"],
          input: "Sample stream", output: "Burst 起點、CFO、gain",
          code: ["<code>ofdm_link.phy.sync</code>：<code>acquire_preamble</code>", "<code>ofdm_link.phy.streaming</code>：<code>StreamingBurstDecoder</code>"],
          opts: ["Profile 的 <code>sync.detection_threshold</code>、<code>correlation_threshold</code>"],
          gui: "RX 的 <code>Decode</code> tab：decode funnel。"
        },
        {
          name: "Header decode",
          does: ["這個 block 用 training symbol 做 channel estimation。", "然後它用 3× repetition 的 majority vote 與 CRC 解出 3 個 header symbols。",
            "CRC 失敗時，它改用 soft combining 再試一次。",
            "Header 提供 MCS、wire version 與 length。所以你不需要在 RX 設定 MCS。"],
          input: "Training 與 header symbols", output: "Channel estimate、MCS、length",
          code: ["<code>ofdm_link.phy.channel</code>：<code>estimate_channel</code>", "<code>ofdm_link.phy.burst_header</code>：<code>decode_burst_header</code>、<code>decode_burst_header_soft</code>"],
          opts: ["—"],
          gui: "RX message list 的每一行顯示 MCS。"
        },
        {
          name: "Equalizer",
          does: ["這個 block 移除 cyclic prefix，然後做 FFT。", "它用 channel estimate 與 4 個 pilots 修正 amplitude 與 phase。",
            "它也追蹤 sample clock offset。"],
          input: "Payload OFDM symbols", output: "Equalized data symbols",
          code: ["<code>ofdm_link.phy.channel</code>：<code>equalize_with_pilots</code>", "<code>ofdm_link.phy.burst</code>：sample clock tracking"],
          opts: ["—"],
          gui: "RX 的 constellation 與 <code>mean EVM</code>。"
        },
        {
          name: "Demapper",
          does: ["這個 block 把 symbols 變回 bits。", "Turbo（MCS 0 與 MCS 4）使用 soft demapping：它計算每個 bit 的 LLR。",
            "Header 的 wire version 決定使用哪一種 demapper。"],
          input: "Equalized symbols", output: "Bits 或 LLRs",
          code: ["<code>ofdm_link.phy.soft</code>：<code>soft_demap_symbols</code>", "<code>ofdm_link.runtime.factory</code>：<code>select_frame_decoder</code>"],
          opts: ["由 header 的 MCS 決定"], gui: "—"
        },
        {
          name: "FEC decoder",
          does: ["這個 block 修正 bit errors。", "Turbo decoder 是 native C++。它最多做 8 次 iterations。",
            "這一步使用最多 CPU 時間。所以 4 個 worker processes 平行 decode。"],
          input: "Bits 或 LLRs", output: "Frame bits",
          code: ["<code>ofdm_link.runtime.segment_decode_pool</code>：<code>SegmentDecodePool</code>", "<code>ofdm_link.phy.turbo</code>"],
          opts: ["<code>--decode-workers</code>（預設 4）"],
          gui: "RX 的 <code>Hardware</code> tab：<code>decode_pool</code> 計數。"
        }
      ]
    },
    {
      id: "rx-link", cls: "rx", area: "rx-link", arrow: "left", row: "RX",
      name: "Link", line: "CRC 檢查、loss 計數、reassembly",
      sum: "Link layer 只接受正確的 frame，然後把 fragments 組回 message。",
      blocks: [
        {
          name: "Frame check",
          does: ["CRC-32 正確時，frame 才算 decode 成功。", "Sequence number 跳號時，表示有 burst 遺失。",
            "RX 無法分辨「沒找到 preamble」與「CRC 失敗」。RX 把兩種情況都算成 loss。"],
          input: "Frame bits", output: "一個 datagram",
          code: ["<code>link.py</code>：<code>MessageReceiver</code>、<code>_SequenceTracker</code>"],
          opts: ["—"],
          gui: "RX 的 <code>Loss (10 s)</code> card 與 link light。"
        },
        {
          name: "Reassembly",
          does: ["這個 block 把 message ID 相同的 fragments 組在一起。", "缺少一個 fragment 時，它丟棄整則 message。",
            "它不會交付不完整的 message。"],
          input: "Datagrams", output: "一則完整的 message",
          code: ["<code>datagram.py</code>：<code>Reassembler</code>"],
          opts: ["—"],
          gui: "RX 的 <code>Decode</code> tab：<code>incomplete messages</code>。"
        }
      ]
    },
    {
      id: "rx-app", cls: "rx", area: "rx-app", arrow: "left", row: "RX",
      name: "Egress", line: "GUI message list 與 UDP port 52002",
      sum: "RX 程式顯示每一則完整的 message，並把它送給其他程式。",
      blocks: [{
        name: "Egress",
        does: ["RX window 在 message list 顯示 message。", "RX 程式也把 message 送到 UDP egress port。",
          "TX 與 RX 在同一台電腦時，window 顯示 end-to-end latency。"],
        input: "一則完整的 message", output: "Window 裡的文字與 UDP bytes",
        code: ["<code>rx_app.py</code>：<code>ReceiveWindow</code>、<code>EgressForwarder</code>"],
        opts: ["<code>--egress-host</code>、<code>--egress-port</code>（預設 127.0.0.1:52002）"],
        gui: "RX 的 <code>Log</code> tab 與 <code>Goodput (2 s)</code> card。"
      }]
    },
    {
      id: "ext-out", cls: "ext", area: "ext-out", row: "目的地",
      name: "你的程式", line: "從 UDP port 接收 bytes",
      sum: "任何能讀 UDP socket 的程式，都能接收 message。",
      blocks: [{
        name: "Consumer",
        does: ["程式在 UDP port 52002 接收。", "RX 每完整 decode 一則 message，程式就收到一則。"],
        input: "UDP datagrams", output: "你的資料",
        code: ["<code>udp_recv.py</code>（最小範例）", "<code>ffplay</code>（video）"],
        opts: ["<code>--egress-port</code>（預設 52002）"], gui: "—"
      }]
    },
    /* ---- optional layer: control and GUI instruments ---- */
    {
      id: "mcs", cls: "ctl", area: "mcs", extra: true, row: "Control",
      name: "MCS selector（0 到 7）", line: "從下一則 message 開始生效",
      sum: "MCS 決定 payload 的 modulation 與 FEC。",
      blocks: [{
        name: "MCS selector",
        does: ["你在 TX window 選擇 8 種格式之一。", "新的 MCS 從下一則 message 開始生效。你不需要停止 radio。",
          "同一則 message 的全部 fragments 使用同一種 MCS。", "TX 把 MCS 寫進 burst header。RX 從 header 讀出 MCS。"],
        input: "你的選擇", output: "TX chain 的 FEC 與 modulation",
        code: ["<code>tx_app.py</code>：<code>MCS for next message</code> 選單", "<code>ofdm_link.phy.mcs_table</code>：<code>MCS_TABLE</code>"],
        opts: ["<code>--mcs-index {0..7}</code>"],
        gui: "RX 的 <code>MCS</code> card 顯示收到的 burst 的格式。"
      }]
    },
    {
      id: "spec", cls: "ins", area: "spec", extra: true, row: "GUI",
      name: "Spectrum · RX level", line: "有沒有 signal？",
      sum: "這個 instrument 使用 raw samples。",
      blocks: [{
        name: "Spectrum 與 RX level",
        does: ["Spectrum 顯示收到的全部 samples。", "沒有 burst decode 成功時，它仍然更新。這時候你最需要它。"],
        input: "Raw samples", output: "Spectrum plot",
        code: ["<code>plots.py</code>：<code>SpectrumPlot</code>"],
        opts: ["<code>--ui-fps</code>（預設 15 Hz）"],
        gui: "RX 的 <code>Signal</code> tab。請看 <code>spread</code>：TX 開關時，這個數值明顯變化。"
      }]
    },
    {
      id: "const", cls: "ins", area: "const", extra: true, row: "GUI",
      name: "Constellation · EVM", line: "Burst 的品質如何？",
      sum: "這個 instrument 只使用 decode 成功的 burst。",
      blocks: [{
        name: "Constellation 與 EVM",
        does: ["這張圖顯示 decode 成功的 burst 的 equalized symbols。", "Link idle 時，它不會顯示 noise。",
          "超過 3 秒沒有新的 burst 時，圖變成灰色，並顯示 <code>stale</code>。"],
        input: "Equalized symbols", output: "Constellation plot、EVM",
        code: ["<code>plots.py</code>：<code>ConstellationPlot</code>", "<code>link.py</code>：<code>measure_quality</code>"],
        opts: ["<code>--ui-fps</code>"],
        gui: "RX 的 <code>Signal</code> tab。"
      }]
    },
    {
      id: "stats", cls: "ins", area: "stats", extra: true, row: "GUI",
      name: "Stats · Link light", line: "Goodput、loss、SNR",
      sum: "這個 instrument 回答一個問題：link 好不好？",
      blocks: [{
        name: "Stats 與 link light",
        does: ["Cards 顯示 goodput、10 秒的 loss 與 2 秒的 SNR。", "Light 顯示 WAITING、IDLE、DOWN、LOSSY、LOSS、MARGINAL 或 GOOD。",
          "SNR 太低時，light 建議另一種 MCS。"],
        input: "Frame 的結果", output: "Cards、trend、light",
        code: ["<code>dashboard.py</code>：<code>rx_light</code>、<code>suggested_mcs</code>"],
        opts: ["<code>--stats-log PATH</code>"],
        gui: "RX 的 cards 與 60 秒 trend。"
      }]
    }
  ];

  // The path of one message. `shape` is a list of [class, label] segments.
  var TOUR = [
    { mod: "ext-in", cap: "程式或 input box 提供 message。", shape: [["s-p", "message bytes"]] },
    { mod: "tx-app", cap: "TX 程式把 message 放進 send queue。", shape: [["s-p", "message bytes"]] },
    { mod: "tx-link", cap: "Link layer 切分 message，並加上兩個 header 與一個 CRC。",
      shape: [["s-h", "frame header 7 B"], ["s-h", "datagram header 28 B"], ["s-p", "fragment ≤ 968 B"], ["s-c", "CRC-32"]] },
    { mod: "tx-phy", cap: "PHY 把 frame encode，然後組成一個 OFDM burst。",
      shape: [["s-h", "preamble"], ["s-h", "training"], ["s-c", "header × 3"], ["s-p", "payload × N OFDM symbols"]] },
    { mod: "tx-tr", cap: "Transport 縮放 burst，然後送出 samples。",
      shape: [["s-z", "256 zeros"], ["s-p", "burst samples，peak 0.7"], ["s-z", "256 zeros"]] },
    { mod: "channel", cap: "Channel 在 samples 上加 noise。", shape: [["s-z", "noise"], ["s-p", "burst + noise"], ["s-z", "noise"]] },
    { mod: "rx-tr", cap: "RX 收到連續的 stream。Burst 在 stream 裡的某個位置。",
      shape: [["s-z", "idle samples"], ["s-p", "burst + noise"], ["s-z", "idle samples"]] },
    { mod: "rx-phy", cap: "PHY 找到 burst，讀出 header，然後 decode payload。",
      shape: [["s-h", "frame header 7 B"], ["s-h", "datagram header 28 B"], ["s-p", "fragment ≤ 968 B"], ["s-c", "CRC-32"]] },
    { mod: "rx-link", cap: "Link layer 檢查 CRC，然後把 fragments 組回 message。", shape: [["s-p", "message bytes"]] },
    { mod: "rx-app", cap: "RX window 顯示 message，並把它送到 UDP port 52002。", shape: [["s-p", "message bytes"]] },
    { mod: "ext-out", cap: "你的程式收到的 bytes 與來源送出的 bytes 相同。", shape: [["s-p", "message bytes"]] }
  ];

  var ACK = "--enable-rf \\\n    --acknowledgement 'I acknowledge that this process will transmit RF'";
  var CAUTION = "第一次請用 cable 與 attenuator 連接兩台 radio。接上 antenna 發射前，請先向指導老師確認可以使用的頻段與功率。RF 發射預設是關閉的。";
  var RUN = {
    sim: {
      title: "Simulation：不需要硬體",
      steps: [
        { text: "開兩個 terminal。在每個 terminal 啟用環境。", cmd: "conda activate ofdm-message-link" },
        { text: "Terminal 1：先啟動 RX。RX 必須先開啟 sample port。", cmd: "python -m ofdm_message_link.rx_app --snr-db 15" },
        { text: "Terminal 2：啟動 TX。", cmd: "python -m ofdm_message_link.tx_app" },
        { text: "在 TX 的 input box 打字，然後按 Enter。RX window 顯示這則 message。" },
        { text: "選用：用其他程式當來源與目的地。", cmd: "python -m ofdm_message_link.udp_recv\npython -m ofdm_message_link.udp_send \"Hello\"" }
      ]
    },
    uhd: {
      title: "USRP：over the air",
      steps: [
        { caution: CAUTION },
        { text: "Terminal 1：先啟動 RX。RX 不發射。", cmd: "python -m ofdm_message_link.rx_app --transport uhd" },
        { text: "Terminal 2：啟動 TX，並帶上 RF acknowledgement。", cmd: "python -m ofdm_message_link.tx_app --transport uhd \\\n    " + ACK },
        { text: "在兩個 window 開啟 Hardware tab。選擇 device、antenna 與 gain。然後按 Start radio。" },
        { text: "TX gain 預設是 0 dB，也就是最小值。請調高 Gain (dB)，直到 RX light 顯示 GOOD。" }
      ]
    },
    pluto: {
      title: "ADALM-Pluto：over the air",
      steps: [
        { caution: CAUTION },
        { text: "安裝 ADI 的 udev rule。只需要做一次。然後重新插拔 Pluto。詳細步驟在 <code>docs/04-ota-hardware.md</code>。" },
        { text: "Terminal 1：先啟動 RX。", cmd: "python -m ofdm_message_link.rx_app \\\n    --transport pluto --serial RX_PLUTO_SERIAL --gain 15 --auto-start" },
        { text: "Terminal 2：啟動 TX，並帶上 RF acknowledgement。", cmd: "python -m ofdm_message_link.tx_app \\\n    --transport pluto --serial TX_PLUTO_SERIAL --gain -5 --auto-start \\\n    " + ACK },
        { text: "Pluto 的 TX gain 是 attenuator：0 dB 是最大輸出。TX 與 RX 請各用一台 Pluto。" }
      ]
    }
  };

  var SYMPTOMS = [
    { q: "TX 的 log 一直增加，但是 RX 沒有顯示", cause: "TX gain 在最小值。", fix: "在 TX 的 <code>Hardware</code> tab 調高 <code>Gain (dB)</code>。新的 gain 立即生效。" },
    { q: "Spectrum 有變化，但是沒有 burst decode 成功", cause: "RX antenna 接到錯誤的 port。<code>rx level</code> 的 <code>spread</code> 很小。", fix: "選擇另一個 antenna port。這比調高 gain 更有效。" },
    { q: "RX 完全找不到 preamble", cause: "TX 與 RX 的 sample rate 不同。", fix: "TX 與 RX 使用相同的 <code>--overlay</code>。" },
    { q: "高速率時遺失很多 packet", cause: "RX gain 太高，signal 太強。", fix: "把 RX gain 降到大約 15 dB。" },
    { q: "Overflow 次數一直增加", cause: "USB bandwidth 或 CPU 不夠。", fix: "把兩台 radio 接到不同的 USB 3 controller。保持 <code>--engine process</code>。" },
    { q: "Probe 失敗：device 被佔用", cause: "另一個 process 已經開啟這台 radio。", fix: "確認 TX 與 RX 的 <code>--serial</code> 不同。停止舊的 process。" },
    { q: "Pluto：device 清單是空的", cause: "電腦沒有在 USB 上找到 Pluto。", fix: "重新插拔 Pluto。Pluto 開機大約需要 20 秒。" }
  ];

  // File map: [files, module ids it implements, what it does].
  var FILES = [
    ["tx_app.py", "Ingress", "TX window、UDP ingress、send thread"],
    ["rx_app.py", "Egress、GUI instruments", "RX window、UDP egress、<code>--stats-log</code>"],
    ["datagram.py", "Datagram、Reassembly", "28-byte header、fragmentation、reassembly"],
    ["link.py", "PHY frame、Frame check", "Bytes 與 burst 的轉換、sequence gap、EVM 與 SNR 量測"],
    ["transport.py", "Sample sink、Sample source", "UDP 與 UHD 的 sample transport、peak normalization、timed TX burst"],
    ["pluto.py", "Sample sink、Sample source", "ADALM-Pluto：經過 libiio 的 discover、probe、sink 與 source"],
    ["engine.py", "（process layout）", "把 radio path 放進 child process"],
    ["options.py", "（TX 與 RX 共用）", "CLI options、config 解析、RF enable gate"],
    ["devices.py、radio_panel.py", "Sample sink、Sample source", "列出並 probe radio、Radio panel"],
    ["dashboard.py", "Stats · Link light", "Link light 規則與 sliding-window 統計。不 import Qt"],
    ["dashboard_widgets.py、plots.py", "GUI instruments", "Cards、trend、constellation、spectrum"],
    ["qt_runtime.py、window_layout.py", "（window）", "Qt 啟動、Ctrl-C、<code>--geometry</code>"],
    ["log_limits.py", "（長時間執行）", "Log 大小上限與 rotation"],
    ["udp_send.py、udp_recv.py", "你的程式", "最小的 socket producer 與 consumer"],
    ["experiments/ota_udp_throughput/", "（量測工具）", "各 MCS 的 airtime 與 OTA UDP throughput sweep"]
  ];

  return { MODULES: MODULES, TOUR: TOUR, RUN: RUN, SYMPTOMS: SYMPTOMS, FILES: FILES };
})();

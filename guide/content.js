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
      name: "Link", line: "Fragment、sequence number、CRC、A-MPDU",
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
            "RX 用 sequence number 計算遺失的 frame 數量。RX 用 CRC 找出 decode 錯誤。"],
          input: "一個 datagram", output: "一個 PHY frame（最多 1007 bytes）",
          code: ["<code>link.py</code>：<code>MessageTransmitter.encode_datagrams()</code>", "<code>ofdm_link.phy.codec</code>：<code>Frame</code>"],
          opts: ["—"], gui: "—"
        },
        {
          name: "Aggregation",
          does: ["這個 block 把排隊中的 frames 放進同一個 burst。這個格式叫 A-MPDU。",
            "每個 frame 前面有 4-byte delimiter。Delimiter 內有 frame 的 length 與 CRC-8。",
            "Burst 的最後有 4-byte FCS。FCS 是整個 PSDU 的 CRC-32。",
            "Radio 可以接受下一個 burst 時，這個 block 放入 queue 裡的 frames。上限是 16 個 frames、8192 bytes 與 8 ms airtime。",
            "兩個 burst 的開始時間至少相隔 4 ms。等待時，這個 block 把新的 frames 放進同一個 burst。所以 burst 數最多每秒 250 個。",
            "比 4 ms 長的 burst 不等待。閒置之後的第一個 burst 也不等待。滿的 burst 不等待。"],
          input: "一個或多個 PHY frames", output: "一個 PSDU",
          code: ["<code>aggregation.py</code>：<code>plan_aggregate</code>", "<code>ofdm_link.phy.ampdu</code>：<code>pack_ampdu</code>"],
          opts: ["<code>--aggregation</code>、<code>--aggregation-max-subframes</code>、<code>--min-burst-interval-ms</code>、<code>--max-burst-airtime-ms</code>"],
          gui: "TX 的 <code>Hardware</code> tab：<code>subframes/burst</code>。"
        }
      ]
    },
    {
      id: "tx-phy", cls: "tx", area: "tx-phy", arrow: "right", row: "TX",
      name: "PHY", line: "PSDU 變成 OFDM burst",
      sum: "PHY 把一個 PSDU 變成一個 burst 的 complex samples。",
      blocks: [
        {
          name: "FEC encoder",
          does: ["這個 block 加入 redundant bits。", "RX 用這些 bits 修正 noise 造成的錯誤。",
            "全部 MCS 使用同一個 Turbo encoder。它的 rate 是 1/3。",
            "然後 LTE rate matching（TS 36.212）刪除或重複一些 bits。所以 code rate 可以是 1/3、1/2、2/3 或 3/4。",
            "Rate matching 讓 coded bits 剛好填滿整數個 OFDM symbols。Payload 沒有 padding。"],
          input: "PSDU bits", output: "Coded bits",
          code: ["<code>ofdm_link.phy.psdu_codec</code>：<code>RateMatchedTurboCodec</code>", "<code>ofdm_link.phy.rate_match</code>"],
          opts: ["由 MCS 決定"],
          gui: "TX 的 <code>MCS for next message</code> 選單顯示 code rate。"
        },
        {
          name: "Scrambler",
          does: ["這個 block 把 coded bits 與一個已知的序列做 XOR。這個序列來自 802.11 的 x⁷+x⁴+1 LFSR。",
            "所以 payload 的內容不會改變 constellation 的統計、PAPR 與發射功率。",
            "TX 每個 burst 使用另一個 seed（1 到 127）。TX 把 seed 寫進 SIG。所以 RX 不需要設定。"],
          input: "Coded bits", output: "Scrambled bits",
          code: ["<code>ofdm_link.phy.scrambler</code>：<code>scramble_bits</code>", "<code>link.py</code>：<code>MessageTransmitter</code>（seed）"],
          opts: ["<code>phy.v5.scrambler</code>（預設 <code>true</code>；<code>false</code> 送 seed 0，只用於實驗）"],
          gui: "—"
        },
        {
          name: "Symbol mapper",
          does: ["這個 block 把 bits 變成 constellation points。", "QPSK 的每個 symbol 帶 2 bits。16QAM 帶 4 bits。64QAM 帶 6 bits。",
            "16QAM 與 64QAM 比較快，但是需要比較高的 SNR。"],
          input: "Coded bits", output: "Complex symbols",
          code: ["<code>ofdm_link.phy.codec</code>：<code>map_symbols</code>"],
          opts: ["由 MCS 決定"],
          gui: "RX 的 constellation 顯示 4 個、16 個或 64 個黃色 reference points。"
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
          does: ["這個 block 依序接上四個部分：STF、LTF、3 個 SIG symbols、payload。",
            "STF 與 LTF 來自 802.11a。RX 用 STF 找 burst，用 LTF 做 channel estimation。",
            "SIG symbols 使用 BPSK 與 K=7、rate 1/2 的 convolutional code。一個 interleaver 把 coded bits 分散到三個 symbols 的 carriers。",
            "SIG 內有 MCS、PSDU length、scrambler seed 與 CRC-16。",
            "Link 在 burst 的前面加 16 個 zero samples，在後面加 64 個。"],
          input: "OFDM symbols", output: "一個 burst",
          code: ["<code>ofdm_link.phy.burst_v5</code>：<code>encode_burst_v5</code>", "<code>ofdm_link.phy.header_v5</code>", "<code>link.py</code>：加上 guard samples"],
          opts: ["<code>phy.v5.guard_pre_samples</code>、<code>guard_post_samples</code>（<code>configs/default.yaml</code>）"],
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
          does: ["v5 burst 的 PAPR 大約是 9.5 到 10 dB。Peak 常常大於 1.0。", "Radio 會 clip 大於 1.0 的 samples。",
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
      name: "PHY", line: "Samples 變回 PSDU",
      sum: "PHY 在 stream 裡找到每個 burst，然後 decode。SIG 告訴 RX 要用哪一種 MCS。RX 也 decode 舊格式（v1–v4）的 burst。",
      blocks: [
        {
          name: "Acquisition",
          does: ["這個 block 計算 samples 與 32 個 samples 之前的 samples 的 correlation。Preamble 重複時，這個 correlation 變大。",
            "一次計算找兩種 burst：v5 與舊格式（v1–v4）。兩種 preamble 都相隔 32 個 samples 重複。v5 的 window 有 128 個 products，舊格式有 32 個。",
            "DC offset 或 CW tone 在每個 product 加上一個穩定的成分。這個 block 先減去這個成分，然後比較門檻。",
            "這個 block 用 STF 估計 coarse CFO，並用 STF 的 coherent correlation 擋掉 tone 與 spur。",
            "然後它用 LTF 的兩個 symbols 找準確的起點與 fine CFO。舊格式的 training symbol 只像第一個 LTF symbol。所以這一步擋掉舊格式的 burst。"],
          input: "Sample stream", output: "Burst 起點、CFO",
          code: ["<code>ofdm_link.phy.sync</code>：<code>acquire_all</code>", "<code>ofdm_link.phy.sync_v5</code>", "<code>ofdm_link.phy.streaming</code>：<code>StreamingBurstDecoder</code>"],
          opts: ["<code>phy.rx.v5_stf_threshold</code>（0.20）、<code>v5_correlation_threshold</code>（0.25）", "舊格式：<code>sync.detection_threshold</code>、<code>correlation_threshold</code>"],
          gui: "RX 的 <code>Decode</code> tab：decode funnel；<code>SNR (2 s)</code> card。"
        },
        {
          name: "Header decode",
          does: ["這個 block 用兩個 LTF symbols 的平均做 channel estimation。Channel 的 impulse response 比 cyclic prefix 短，所以它把 estimate 平滑成 20 個 taps，去掉大部分的 noise。",
            "LTF 的兩個 symbols 相同。相同的部分是 signal，不同的部分是 noise。這個 block 只看 data carriers 所在的頻帶。所以每個 burst 都有一個 SNR，就算後面 decode 失敗也一樣。",
            "然後它用 Viterbi decoder 與 CRC-16 解出 3 個 SIG symbols。",
            "CRC 失敗時，RX 丟棄這個 burst。SIG 比最穩的 payload 更耐 noise，所以不需要其他方法。",
            "SIG 解出來之後，它的 symbols 是已知的。這個 block 用它們量出 payload 會遇到的 noise。",
            "SIG 提供 MCS、length 與 scrambler seed。所以你不需要在 RX 設定 MCS。",
            "舊格式的 burst 使用舊的 header decoder。它有 majority vote、soft combining 與 maximum likelihood。"],
          input: "LTF 與 SIG symbols", output: "Channel estimate、noise variance、MCS、length",
          code: ["<code>ofdm_link.phy.sync_v5</code>：<code>measure_ltf_pair</code>", "<code>ofdm_link.phy.channel</code>：<code>refine_channel_estimate</code>", "<code>ofdm_link.phy.header_v5</code>：<code>decode_v5_header</code>", "舊格式：<code>ofdm_link.phy.burst_header</code>"],
          opts: ["—"],
          gui: "RX message list 的每一行顯示 MCS。"
        },
        {
          name: "Equalizer",
          does: ["這個 block 移除 cyclic prefix，然後做 FFT。每個 FFT window 在 cyclic prefix 裡提早 3 個 samples 開始。所以 timing 晚一點也不會混進下一個 symbol。",
            "它用 channel estimate 與 4 個 pilots 修正 amplitude 與 phase。",
            "它也追蹤 sample clock offset。Pilots 的 phase slope 沿著時間 unwrap。Drift 變大時，它把 FFT window 移動整數個 samples。"],
          input: "Payload OFDM symbols", output: "Equalized data symbols",
          code: ["<code>ofdm_link.phy.channel</code>：<code>equalize_with_pilots</code>", "<code>ofdm_link.phy.burst</code>：sample clock tracking"],
          opts: ["<code>phy.rx.fft_backoff_samples</code>（預設 3）"],
          gui: "RX 的 constellation 與 <code>mean EVM</code>。"
        },
        {
          name: "Demapper",
          does: ["這個 block 把 symbols 變回 LLRs。",
            "它用一個 noise variance 計算每個 bit 的 LLR。這個值是 SIG 量到的 noise 與 LTF 量到的 noise 中比較大的那個。",
            "SIG 的 MCS 決定使用哪一種 demapper：QPSK、16QAM 或 64QAM。"],
          input: "Equalized symbols", output: "LLRs",
          code: ["<code>ofdm_link.phy.soft</code>：<code>soft_demap_symbols</code>", "<code>ofdm_link.phy.burst_v5</code>：<code>_sig_noise_variance</code>"],
          opts: ["由 SIG 的 MCS 決定", "<code>phy.rx.llr_noise_source</code>（預設 <code>max</code>，只用於實驗）"], gui: "—"
        },
        {
          name: "Descrambler",
          does: ["這個 block 用 SIG 的 scrambler seed 產生同一個序列。",
            "序列的 bit 是 1 時，它把 LLR 的正負號反過來。所以 FEC decoder 收到 scrambling 之前的 LLRs。"],
          input: "LLRs", output: "LLRs",
          code: ["<code>ofdm_link.phy.scrambler</code>：<code>descramble_llrs</code>"],
          opts: ["由 SIG 的 scrambler seed 決定"], gui: "—"
        },
        {
          name: "FEC decoder",
          does: ["這個 block 修正 bit errors。", "它先把 LLRs 放回 rate matching 之前的位置。被刪除的位置填 0。",
            "Turbo decoder 是 native C++。它最多做 8 次 iterations。Code block 的 CRC 正確時，它提早停止。",
            "這一步使用最多 CPU 時間。所以 4 個 worker processes 平行 decode。一個 worker decode 一個 burst。",
            "最後它檢查整個 PSDU 的 FCS。"],
          input: "LLRs", output: "PSDU bytes 與 FCS 的結果",
          code: ["<code>ofdm_link.runtime.segment_decode_pool</code>：<code>SegmentDecodePool</code>", "<code>ofdm_link.phy.turbo</code>"],
          opts: ["<code>--decode-workers</code>（預設 4）", "<code>phy.rx.turbo_max_iterations</code>（預設 8）"],
          gui: "RX 的 <code>Hardware</code> tab：<code>decode_pool</code> 計數。"
        }
      ]
    },
    {
      id: "rx-link", cls: "rx", area: "rx-link", arrow: "left", row: "RX",
      name: "Link", line: "A-MPDU unpack、CRC 檢查、loss 計數、reassembly",
      sum: "Link layer 只接受正確的 frame，然後把 fragments 組回 message。",
      blocks: [
        {
          name: "A-MPDU unpack 與 frame check",
          does: ["這個 block 用 delimiters 把 PSDU 分成 frames。Delimiter 壞了時，它每 4 bytes 找下一個 delimiter。",
            "CRC-32 正確時，frame 才算 decode 成功。FCS 失敗時，CRC-32 正確的 frames 仍然交付。",
            "Sequence number 跳號時，表示有 frame 遺失。",
            "RX 無法分辨「沒找到 preamble」與「CRC 失敗」。RX 把兩種情況都算成 loss。"],
          input: "PSDU bytes", output: "一個或多個 datagrams",
          code: ["<code>ofdm_link.phy.ampdu</code>：<code>unpack_ampdu</code>", "<code>link.py</code>：<code>MessageReceiver</code>、<code>_SequenceTracker</code>"],
          opts: ["—"],
          gui: "RX 的 <code>Loss (10 s)</code> card 與 link light。RX 的 <code>Decode</code> tab：PSDU、subframe 與 delimiter 的計數。"
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
      name: "MCS selector（8 到 17）", line: "從下一則 message 開始生效",
      sum: "MCS 決定 payload 的 modulation 與 FEC。",
      blocks: [{
        name: "MCS selector",
        does: ["你在 TX window 選擇一種 v5 格式：QPSK、16QAM 或 64QAM，code rate 1/3 到 3/4。", "新的 MCS 從下一則 message 開始生效。你不需要停止 radio。",
          "同一則 message 的全部 fragments 使用同一種 MCS。", "TX 把 MCS 寫進 SIG。RX 從 SIG 讀出 MCS。",
          "MCS 0 到 7 是舊格式。TX 不送舊格式。RX 仍然 decode 舊格式。"],
        input: "你的選擇", output: "TX chain 的 FEC 與 modulation",
        code: ["<code>tx_app.py</code>：<code>MCS for next message</code> 選單", "<code>ofdm_link.phy.mcs_table</code>：<code>v5_entries</code>"],
        opts: ["<code>--mcs-index N</code>（8 到 17，預設 8）"],
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
    { mod: "tx-link", cap: "Link layer 切分 message，並加上兩個 header 與一個 CRC。然後它把 frames 放進一個 PSDU。",
      shape: [["s-h", "delimiter 4 B"], ["s-h", "frame header 7 B"], ["s-h", "datagram header 28 B"], ["s-p", "fragment ≤ 968 B"], ["s-c", "CRC-32"], ["s-c", "FCS"]] },
    { mod: "tx-phy", cap: "PHY 把 PSDU encode，然後組成一個 OFDM burst。",
      shape: [["s-h", "STF"], ["s-h", "LTF"], ["s-c", "SIG × 3"], ["s-p", "payload × N OFDM symbols"]] },
    { mod: "tx-tr", cap: "Transport 縮放 burst，然後送出 samples。",
      shape: [["s-z", "16 zeros"], ["s-p", "burst samples，peak 0.7"], ["s-z", "64 zeros"]] },
    { mod: "channel", cap: "Channel 在 samples 上加 noise。", shape: [["s-z", "noise"], ["s-p", "burst + noise"], ["s-z", "noise"]] },
    { mod: "rx-tr", cap: "RX 收到連續的 stream。Burst 在 stream 裡的某個位置。",
      shape: [["s-z", "idle samples"], ["s-p", "burst + noise"], ["s-z", "idle samples"]] },
    { mod: "rx-phy", cap: "PHY 找到 burst，讀出 SIG，然後 decode payload。結果是一個 PSDU。",
      shape: [["s-h", "delimiter 4 B"], ["s-h", "frame header 7 B"], ["s-h", "datagram header 28 B"], ["s-p", "fragment ≤ 968 B"], ["s-c", "CRC-32"], ["s-c", "FCS"]] },
    { mod: "rx-link", cap: "Link layer 把 PSDU 分成 frames，檢查 CRC，然後把 fragments 組回 message。", shape: [["s-p", "message bytes"]] },
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
        { text: "選用：用其他程式當來源與目的地。", cmd: "python -m ofdm_message_link.udp_recv\npython -m ofdm_message_link.udp_send \"Hello\"" },
        { text: "選用：雙向 link。<code>node_app</code> 在一個 process 裡同時發送與接收。每個 node 用一個 terminal。Node 1 與 node 2 經過 UDP 交換 samples。",
          cmd: "python -m ofdm_message_link.node_app --node-id 1\npython -m ofdm_message_link.node_app --node-id 2" },
        { text: "Node 1 從 UDP port 52211 收 bytes。Node 2 把這些 bytes 送到 UDP port 52222。反方向用 52221 與 52212。" },
        { text: "每個 node 用 <code>olla</code>（預設的 link adaptation）為每個 burst 選 MCS。<code>--link-adaptation fixed</code> 固定在 <code>--mcs-index</code>。第 5 個分頁說明 node 裡面的 blocks。" }
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
        { text: "Pluto 的 TX gain 是 attenuator：0 dB 是最大輸出。TX 與 RX 請各用一台 Pluto。" },
        { text: "選用：雙向 FDD link。每台 Pluto 執行一個 <code>node_app</code>。Node 1 在 2420 MHz 發送，在 2460 MHz 接收。Node 2 相反。",
          cmd: "python -m ofdm_message_link.node_app --node-id 1 \\\n    --transport pluto --serial NODE1_PLUTO_SERIAL --tx-gain -10 --auto-start \\\n    " + ACK +
            "\npython -m ofdm_message_link.node_app --node-id 2 \\\n    --transport pluto --serial NODE2_PLUTO_SERIAL --tx-gain -10 --auto-start \\\n    " + ACK },
        { text: "一台 Pluto 只能開啟一次。所以一個 <code>node_app</code> 同時擁有這台 Pluto 的 TX 與 RX。<code>--headless</code> 不開 window，供腳本使用。" }
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
    { q: "Pluto：device 清單是空的", cause: "電腦沒有在 USB 上找到 Pluto。", fix: "重新插拔 Pluto。Pluto 開機大約需要 20 秒。" },
    { q: "node_app：TX duty 高時，overflow 次數增加", cause: "USB 2.0 同時傳送 5 MS/s 的 RX stream 與 TX bursts。TX duty 超過大約 0.7 時，bandwidth 不夠。",
      fix: "保持 <code>--tx-duty-cap 0.7</code>（預設值）。" },
    { q: "node_app：Feedback age 是紅色", cause: "另一個方向的 SNR 太低。Peer 收不到這個 node 的 feedback。",
      fix: "調高 peer 的 <code>--tx-gain</code>。兩個方向的 path loss 可以不同。" },
    { q: "node_app：EADDRINUSE", cause: "另一個 process 已經使用這個 node 的 port。", fix: "停止舊的 process。兩個 node 必須用不同的 <code>--node-id</code>。" }
  ];

  // File map: [files, module ids it implements, what it does].
  var FILES = [
    ["tx_app.py", "Ingress", "TX window、UDP ingress、send thread"],
    ["rx_app.py", "Egress、GUI instruments", "RX window、UDP egress、<code>--stats-log</code>"],
    ["datagram.py", "Datagram、Reassembly", "28-byte header、fragmentation、reassembly"],
    ["link.py", "PHY frame、Frame check", "Bytes 與 burst 的轉換、sequence gap、EVM 與 SNR 量測"],
    ["aggregation.py", "Aggregation", "一個 burst 放幾個 frames"],
    ["transport.py", "Sample sink、Sample source", "UDP 與 UHD 的 sample transport、peak normalization、timed TX burst"],
    ["pluto.py", "Sample sink、Sample source", "ADALM-Pluto：經過 libiio 的 discover、probe、sink 與 source"],
    ["engine.py", "（process layout）", "把 radio path 放進 child process"],
    ["options.py", "（TX 與 RX 共用）", "CLI options、config 解析、RF enable gate"],
    ["devices.py、radio_panel.py", "Sample sink、Sample source", "列出並 probe radio、Radio panel"],
    ["dashboard.py", "Stats · Link light", "Link light 規則與 sliding-window 統計。不 import Qt"],
    ["dashboard_widgets.py、plots.py", "GUI instruments", "Cards、trend、constellation、spectrum"],
    ["qt_runtime.py、window_layout.py", "（window）", "Qt 啟動、Ctrl-C、<code>--geometry</code>"],
    ["log_limits.py", "（長時間執行）", "Log 大小上限與 rotation"],
    ["node_app.py、node_options.py", "（雙向 node）", "雙向 node 的 CLI、headless loop、control socket、stats line"],
    ["node_engine.py", "（雙向 node）", "一個 child process 同時執行 TX 與 RX，兩個方向共用一台 Pluto"],
    ["node_window.py", "（雙向 node）", "TX、RX 與 link adaptation 的 cards，以及 MCS trend"],
    ["control.py", "（雙向 node）", "LCP：CONTROL frame 的 header 與 FEEDBACK body"],
    ["la/", "（雙向 node）", "LinkController、可以替換的 LinkAdaptation（預設 olla）與 FeedbackReporter、threshold 表"],
    ["udp_send.py、udp_recv.py", "你的程式", "最小的 socket producer 與 consumer"],
    ["experiments/ota_udp_throughput/", "（量測工具）", "各 MCS 的 airtime 與 OTA UDP throughput sweep"],
    ["experiments/tx_v5/", "（量測工具）", "Wire v5 的錄音、goodput 與 acquisition 門檻量測"],
    ["experiments/duplex_fdd/", "（量測工具）", "雙向 node 的 USB、自干擾、固定 MCS、LA 與影像量測"]
  ];

  /* ---- 5. duplex node_app: the blocks of the second diagram ---- */
  // A value that differs between the UDP simulation and a Pluto.  node_app
  // refuses --transport uhd, so the USRP choice shows the Pluto text.
  function ota(sim, hw) { return { sim: sim, uhd: hw, pluto: hw }; }

  var NODE_MODULES = [
    {
      id: "n-parent", cls: "ext", row: "Process",
      name: "node_app main process", sum: "Main process 顯示 window，或執行 headless loop。Radio、codec 與 LA 在 engine child process。",
      blocks: [
        {
          name: "Window 或 headless",
          does: ["沒有 <code>--headless</code> 時，main process 開一個 Qt window。Window 有 TX、RX、LA 三排 cards 與 60 秒的 MCS trend。",
            "<code>--headless</code> 不開 window。它隱含 <code>--auto-start</code>。Pluto 也需要 <code>--serial</code>。",
            "Main process 每 0.5 s 寫一行 stats：<code>tx</code>、<code>rx</code>、<code>la</code>、<code>feedback</code>、<code>cpu</code>。",
            "Radio 沒有啟動時，headless 程式的 exit code 是 2。Engine child process 中途結束時，exit code 是 3。"],
          input: "Engine child process 的 events", output: "Cards、trend、stats line",
          code: ["<code>node_app.py</code>：<code>main</code>、<code>HeadlessNode</code>、<code>NodeMonitor</code>", "<code>node_window.py</code>：<code>NodeWindow</code>"],
          opts: ["<code>--headless</code>、<code>--duration S</code>、<code>--stats-log PATH</code>、<code>--ready-file PATH</code>"],
          gui: "Window 的標題是 <code>OFDM node 1</code>（或 2）。"
        },
        {
          name: "Control socket",
          does: ["Main process 在 localhost 開一個 UDP socket。一個 datagram 是一個 JSON request。每個 request 得到一個 JSON answer。",
            "Commands：<code>set_gain</code>、<code>set_mcs</code>、<code>set_la</code>、<code>mark</code>、<code>stats</code>、<code>stop</code>。",
            "Node 1 用 port 52213，node 2 用 port 52223。Socket 不設 <code>SO_REUSEADDR</code>。Port 已被佔用時，程式停止。",
            "Window 模式也開這個 socket。"],
          input: "JSON request", output: "JSON answer",
          code: ["<code>node_app.py</code>：<code>ControlServer</code>、<code>control_handler</code>"],
          opts: ["<code>--control-port P</code>（預設依 node；0 = 關閉）"],
          gui: "—"
        },
        {
          name: "Gain profile",
          does: ["<code>--gain-profile</code> 依 ready 之後的秒數改變 TX gain，例如 <code>0:-5,30:-20,60:-5</code>。",
            "Window 模式與 headless 都執行 gain profile。", "Main process 把每一步寫進 stats log。"],
          input: "時間與 gain 的清單", output: "TX gain 的改變",
          code: ["<code>node_options.py</code>：<code>parse_gain_profile</code>、<code>GainSchedule</code>"],
          opts: ["<code>--gain-profile T:DB,...</code>、<code>--tx-gain</code>（預設 −89.75 dB）、<code>--rx-gain</code>（預設 15 dB）"],
          gui: "<code>Hardware</code> tab：TX gain。"
        }
      ]
    },
    {
      id: "n-tx-in", cls: "tx", row: "TX",
      name: "Ingress", sum: "Node 從 UDP ingress port 與 <code>Send</code> tab 收 message。",
      blocks: [{
        name: "Ingress",
        does: ["其他程式把 bytes 送到 ingress port。Node 1 用 52211，node 2 用 52221。", "你也可以在 <code>Send</code> tab 打字。",
          "Ingress socket 不設 <code>SO_REUSEADDR</code>。另一個 process 已經使用這個 port 時，radio 不啟動。",
          "Send queue 滿時，node 丟棄 datagram，並計數（<code>ingress_dropped_queue_full</code>）。"],
        input: "UDP datagrams 或打字的文字", output: "一則 message",
        code: ["<code>tx_app.py</code>：<code>IngressListener</code>、<code>TransmitWorker</code>", "<code>node_engine.py</code>：<code>_node_main</code>"],
        opts: ["<code>--ingress-port</code>（預設依 node）"],
        gui: "<code>Send</code> tab。<code>Queue</code> card 顯示 queue 裡的 message 數量。"
      }]
    },
    {
      id: "n-tx-link", cls: "tx", row: "TX",
      name: "Link TX", sum: "Link layer 把 datagrams 放進一個 A-MPDU。每個 burst 的第一個 frame 是 CONTROL frame。",
      blocks: [{
        name: "Link TX",
        does: ["這個 block 與 <code>tx_app</code> 的 Link 相同：fragment、sequence number、CRC-32、A-MPDU。",
          "BurstPolicy 給它一個 CONTROL payload。這個 payload 變成 burst 的第一個 frame（<code>FrameKind.CONTROL</code>）。",
          "CONTROL frame 用自己的 16-bit sequence counter。所以 DATA 的 sequence 保持連續。",
          "CONTROL frame 算進 aggregation 的上限：subframes、bytes 與 airtime。",
          "同一個 MCS 用在 aggregation 的規劃與 encode。所以 MCS 改變不會讓 burst 超過 airtime 上限。"],
        input: "Datagrams 與一個 CONTROL payload", output: "一個 PSDU",
        code: ["<code>link.py</code>：<code>MessageTransmitter.encode_datagrams(control_payloads=...)</code>", "<code>tx_app.py</code>：<code>TransmitWorker</code>、<code>ControlPlan</code>"],
        opts: ["<code>--aggregation</code>、<code>--min-burst-interval-ms</code>、<code>--max-burst-airtime-ms</code>"],
        gui: "<code>Airtime (2 s)</code> card。"
      }]
    },
    {
      id: "n-tx-phy", cls: "tx", row: "TX",
      name: "PHY TX（v5）", sum: "PHY 與 <code>tx_app</code> 相同。只有 scrambler seed 的範圍不同。",
      blocks: [{
        name: "PHY TX",
        does: ["這個 block 把 PSDU 變成一個 v5 burst：Turbo、rate matching、scrambler、QAM、OFDM、STF、LTF 與 SIG。",
          "MCS 來自 LinkAdaptation。每個 data burst 可以用不同的 MCS。",
          "Node 1 只用 scrambler seed 1–63。Node 2 只用 64–127。SIG 帶 seed，所以 RX 從 SIG 知道哪個 node 送了這個 burst。",
          "<code>tx_app</code> 仍然用 1–127。"],
        input: "PSDU 與 MCS", output: "Burst samples",
        code: ["<code>link.py</code>：<code>node_seed_range</code>、<code>NODE_SEED_RANGES</code>", "<code>ofdm_link.phy.burst_v5</code>：<code>encode_burst_v5</code>"],
        opts: ["<code>--node-id {1,2}</code>（決定 seed 範圍）"],
        gui: "<code>TX MCS</code> card。"
      }]
    },
    {
      id: "n-tx-sink", cls: "tx", row: "TX",
      name: "TX sink", sum: ota("UDP sink 把 burst 送到 peer。", "Pluto sink 推送 burst。Duty cap 保護 RX stream。"),
      blocks: [{
        name: "TX sink",
        does: ota(["這個 block 縮放每個 burst，讓 peak 是 0.7。", "UDP sink 把 samples 送到 peer 的 sample port。Node 1 送到 52224，node 2 送到 52214。",
          "UDP transport 沒有 duty cap。"],
          ["這個 block 縮放每個 burst，讓 peak 是 0.7。然後它把 samples 推到 Pluto。",
            "<code>DutyCappedSink</code> 讓 TX duty 不超過 0.7。一個 burst 最早在前一個 burst 開始之後 airtime ÷ 0.7 開始。",
            "原因：Pluto 的 USB 2.0 同時帶 5 MS/s 的 RX stream。TX duty 超過大約 0.7 時，RX overflow（M0 量測）。",
            "閒置的時間最多存 50 ms 的 credit。所以閒置之後的幾個 burst 不必等。"]),
        input: "Burst samples", output: ota("UDP sample datagrams", "Pluto 的 TX buffer"),
        code: ota(["<code>node_engine.py</code>：<code>UdpDuplexFactory</code>", "<code>transport.py</code>：<code>UdpSampleSink</code>"],
          ["<code>node_engine.py</code>：<code>DutyCappedSink</code>、<code>PlutoDuplexFactory</code>", "<code>pluto.py</code>：<code>PlutoSampleSink</code>"]),
        opts: ota(["<code>--udp-peer-port</code>、<code>--peak-amplitude</code>（預設 0.7）"],
          ["<code>--tx-duty-cap D</code>（預設 0.7；1 = 沒有上限）", "<code>--peak-amplitude</code>（預設 0.7）、<code>--tx-gain</code>（預設 −89.75 dB）"]),
        gui: "<code>Hardware</code> tab：TX gain 與原始統計。"
      }]
    },
    {
      id: "n-radio", cls: "sh", row: "Radio",
      name: "Shared radio", sum: ota("兩個 UDP sample sockets 取代 radio。", "一個 Pluto context 同時帶 TX 與 RX。"),
      blocks: [{
        name: "Shared radio",
        does: ota(["<code>--transport udp</code> 不需要 radio。這是預設的 transport。",
          "每個 node 開兩個 UDP sample sockets：一個送到 peer 的 sample port，一個收自己的 sample port。",
          "Node 1 收在 52214，node 2 收在 52224。所以兩個 node 在同一台 PC 上組成一條雙向 link。",
          "<code>--snr-db</code> 在 RX 加 AWGN。"],
          ["<code>node_app</code> 只驅動 Pluto。<code>--transport uhd</code> 會被拒絕。",
            "一個 Pluto 只能開一個 libiio context。第二個 context 失敗（EBUSY）。所以一個 engine child process 同時執行 TX 與 RX。",
            "<code>SharedPlutoContext</code> 把一個 context 借給 sink 與 source。Attribute 與 register 的呼叫經過一把 lock。Buffer 的 refill 與 push 不經過 lock。",
            "Sample rate 只設一次。TX 與 RX 共用它。",
            "Node 先啟動 TX，再啟動 RX。停止時也先停 TX。所以 context 關閉前，TX attenuator 已經回到最小輸出。",
            "Node 1 的 TX LO 是 2.420 GHz，RX LO 是 2.460 GHz。Node 2 相反。"]),
        input: "TX bursts", output: "RX sample stream",
        code: ota(["<code>node_engine.py</code>：<code>UdpDuplexFactory</code>、<code>DuplexEndpoint</code>"],
          ["<code>pluto.py</code>：<code>SharedPlutoContext</code>", "<code>node_engine.py</code>：<code>PlutoDuplexFactory</code>、<code>NodeWorker</code>"]),
        opts: ota(["<code>--udp-listen-port</code>、<code>--udp-peer-port</code>、<code>--snr-db</code>"],
          ["<code>--serial</code>、<code>--rx-kernel-buffers N</code>（預設 4）、<code>--rx-analog-bw HZ</code>", "<code>--enable-rf</code> 與 <code>--acknowledgement</code>（必要）"]),
        gui: "<code>Hardware</code> tab：RX 的 Radio panel、TX gain 與 TX 頻率。"
      }]
    },
    {
      id: "n-ch", cls: "ch", row: "Channel",
      name: "兩個方向", sum: "A → B 與 B → A 同時使用。FDD 讓兩個方向不互相等待。",
      blocks: [
        {
          name: "A → B",
          does: ota(["Node 1 把 samples 送到 UDP port 52224。Node 2 在這個 port 收。", "<code>--snr-db</code> 在 RX 加 AWGN。"],
            ["Node 1 在 2.420 GHz 發送。Node 2 在 2.420 GHz 接收。",
              "兩個頻率相隔 40 MHz。M1 量測沒有看到 self-interference。",
              "2.420 GHz 在 Wi-Fi channel 1 裡。LA 把 Wi-Fi 看成 channel loss。",
              "備用的 profile <code>duplex_fdd_2405_2475.yaml</code> 用 2.405 GHz 與 2.475 GHz。"]),
          input: "Node 1 的 bursts", output: "Node 2 收到的 samples",
          code: ["<code>configs/profiles/duplex_fdd_2420_2460.yaml</code>：<code>duplex.nodes</code>"],
          opts: ["<code>--overlay</code>（預設這個 profile）、<code>--tx-freq</code>、<code>--rx-freq</code>"],
          gui: "Node 2 的 RX cards。"
        },
        {
          name: "B → A",
          does: ota(["Node 2 把 samples 送到 UDP port 52214。Node 1 在這個 port 收。", "<code>--snr-db</code> 在 RX 加 AWGN。"],
            ["Node 2 在 2.460 GHz 發送。Node 1 在 2.460 GHz 接收。",
              "2.460 GHz 在 Wi-Fi channel 11 裡。",
              "兩個方向的 path loss 可以不同。量測時，B → A 需要比 A → B 多大約 6 dB 的 TX gain。"]),
          input: "Node 2 的 bursts", output: "Node 1 收到的 samples",
          code: ["<code>configs/profiles/duplex_fdd_2420_2460.yaml</code>：<code>duplex.nodes</code>"],
          opts: ["<code>--tx-freq</code>、<code>--rx-freq</code>"],
          gui: "Node 1 的 RX cards。"
        }
      ]
    },
    {
      id: "n-feedback", cls: "ctl", row: "Control",
      name: "Feedback（CONTROL frame）", sum: "Feedback 附在每個 burst 的第一個 frame。Node 閒置時，它送一個 control-only burst。",
      blocks: [
        {
          name: "位置",
          does: ["每個 burst 的第一個 frame 是 CONTROL frame。它的 payload 是一個 LCP（Link Control PDU）。",
            "Reporter 有 report 到期時，LCP 帶 FEEDBACK body。沒有時，LCP 只有 8-byte header（LINK_HEADER）。",
            "Node 閒置而且 report 到期時，它送一個 control-only burst。這個 burst 只有 CONTROL frame，用 MCS 8。",
            "Data 的 MCS 高於 9 時，每 100 ms 至少有一份 report 走 MCS 9 以下的 burst。所以 SNR 下降時，feedback 仍然到得了。"],
          input: "FeedbackReport", output: "CONTROL frame",
          code: ["<code>la/controller.py</code>：<code>control_plan</code>", "<code>tx_app.py</code>：<code>ControlPlan</code>"],
          opts: ["<code>--control-mcs-index N</code>（預設 profile 的值，8）"],
          gui: "<code>Feedback age</code> card。"
        },
        {
          name: "LCP header",
          does: ["Header 有 8 bytes：magic <code>0x4C43</code>、version 1、pdu_type、src_node、dst_node、flags、body_length。",
            "pdu_type 1 是 LINK_HEADER，2 是 FEEDBACK，3 保留給 ARQ。",
            "Version 不是 1 時，receiver 忽略整個 LCP，並計數（<code>control_unsupported</code>）。",
            "src_node 等於自己的 node id 時，整個 burst 是自己 TX 的洩漏。Receiver 丟棄它。"],
          input: "Header 欄位", output: "8 bytes",
          code: ["<code>control.py</code>：<code>encode_lcp</code>、<code>decode_lcp</code>"],
          opts: ["—"], gui: "—"
        },
        {
          name: "FEEDBACK body",
          does: ["固定部分有 36 bytes：report_seq、SNR（mean、p10、last）、累計的 DATA ok 與 lost、RX health 計數。",
            "每個最近的 DATA burst 加 6 bytes，最多 16 個。",
            "TLV 0x10 帶 reporter 的 session id。Peer 重新啟動時，LA 立即重設基準。",
            "計數器是累計值。所以遺失幾份 report 不會遺失資訊。",
            "有 4 個 entries 的 FEEDBACK frame 在 burst 裡佔 92 bytes。LINK_HEADER frame 佔 24 bytes。"],
          input: "FeedbackReport", output: "LCP body",
          code: ["<code>control.py</code>：<code>encode_feedback</code>、<code>decode_feedback</code>", "<code>la/api.py</code>：<code>FeedbackReport</code>"],
          opts: ["—"], gui: "—"
        }
      ]
    },
    {
      id: "n-rx-src", cls: "rx", row: "RX",
      name: "RX source", sum: "RX source 提供連續的 sample stream。",
      blocks: [{
        name: "RX source",
        does: ota(["UDP source 在自己的 sample port 收 samples：node 1 用 52214，node 2 用 52224。", "沒有 samples 時，它提供 zeros 或 AWGN。"],
          ["Pluto source 經過 shared context 提供連續的 sample stream。", "RX gain 預設是 15 dB。"]),
        input: "Samples", output: "連續的 sample stream",
        code: ota(["<code>transport.py</code>：<code>UdpSampleSource</code>"], ["<code>pluto.py</code>：<code>PlutoSampleSource</code>"]),
        opts: ota(["<code>--udp-listen-port</code>、<code>--snr-db</code>"], ["<code>--rx-gain</code>（預設 15 dB）、<code>--rx-kernel-buffers</code>"]),
        gui: "<code>Live</code> tab：spectrum。"
      }]
    },
    {
      id: "n-rx-phy", cls: "rx", row: "RX",
      name: "PHY RX（v5）", sum: "PHY 與 <code>rx_app</code> 相同。它也丟棄自己 TX 的洩漏。",
      blocks: [
        {
          name: "Acquisition 與 decode",
          does: ["這個 block 與 <code>rx_app</code> 的 PHY 相同：acquisition、SIG decode、equalizer、demapper、descrambler、Turbo decoder。",
            "3 個 worker processes decode payload。<code>rx_app</code> 預設用 4 個。兩個 node 在同一台 PC 時，3 個符合 CPU 預算。",
            "Receiver 量測每個 peer burst 的 preamble SNR。Reporter 收到這些 SNR。"],
          input: "Sample stream", output: "PSDU bytes",
          code: ["<code>link.py</code>：<code>MessageReceiver</code>", "<code>rx_app.py</code>：<code>ReceiveWorker</code>"],
          opts: ["<code>--decode-workers N</code>（預設 3）"],
          gui: "<code>Live</code> tab：constellation。<code>SNR (2 s)</code> card。"
        },
        {
          name: "丟棄自己的洩漏",
          does: ["自己的 RX 也收到一點自己的 TX。",
            "SIG 的 scrambler seed 在自己的範圍時（node 1：1–63，node 2：64–127），receiver 在 payload decode 之前丟棄整個 burst。這樣省下 Turbo decode 的 CPU。",
            "CONTROL frame 的 src_node 是第二道檢查。",
            "被丟棄的 burst 不進 loss 計數。它的 SNR 也不進給 peer 的 report。"],
          input: "SIG", output: "接受或丟棄",
          code: ["<code>link.py</code>：<code>MessageReceiver(own_seed_range=...)</code>、<code>drain_peer_snr_db</code>"],
          opts: ["<code>--node-id</code>"],
          gui: "Stats line：<code>rx.own_seed_drops</code>、<code>rx.own_bursts_heard</code>。"
        }
      ]
    },
    {
      id: "n-rx-link", cls: "rx", row: "RX",
      name: "Link RX", sum: "Link layer 檢查 frames，把 CONTROL frame 交給 controller，然後組回 messages。",
      blocks: [{
        name: "Link RX",
        does: ["這個 block 把 PSDU 分成 frames，並檢查 CRC-32。",
          "CONTROL frame 不是 datagram。Receiver 把它交給 ControlHandler。",
          "DATA frames 經過 reassembly 變成 messages。",
          "Receiver 為每個 DATA burst 做一個 <code>BurstReport</code>：第一個 sequence、MCS、SNR、ok 與 lost subframes。",
          "Sequence 跳超過 1024 時，receiver 等下一個 sequence 確認。這種跳號不算 loss。"],
        input: "PSDU bytes", output: "Messages、CONTROL payloads、burst reports",
        code: ["<code>link.py</code>：<code>MessageReceiver</code>、<code>ControlContext</code>、<code>ControlVerdict</code>", "<code>datagram.py</code>：<code>Reassembler</code>"],
        opts: ["—"],
        gui: "<code>Loss (10 s)</code> card 與 Link light。"
      }]
    },
    {
      id: "n-rx-out", cls: "rx", row: "RX",
      name: "Egress", sum: "Node 把每一則完整的 message 送給其他程式。",
      blocks: [{
        name: "Egress",
        does: ["Node 把每一則完整的 message 送到 egress port。Node 1 用 52212，node 2 用 52222。",
          "<code>Log</code> tab 列出每一則 message。", "語意是 UDP：遺失的 message 不會重送。"],
        input: "一則完整的 message", output: "UDP bytes",
        code: ["<code>rx_app.py</code>：<code>EgressForwarder</code>"],
        opts: ["<code>--egress-host</code>、<code>--egress-port</code>（預設依 node）"],
        gui: "<code>Log</code> tab 與 <code>Rx goodput (2 s)</code> card。"
      }]
    },
    {
      id: "n-ctrl", cls: "ctl", row: "Control",
      name: "LinkController", sum: "LinkController 把 link adaptation 接到 link layer。它實作三個 hooks。",
      blocks: [
        {
          name: "LinkController",
          does: ["每個 node 有一個 LinkController。它擁有 LinkAdaptation 與 FeedbackReporter。",
            "它實作三個 hooks：BurstPolicy、ControlHandler 與 ReceiveObserver。",
            "全部的呼叫在一把 lock 下執行。所以演算法與 reporter 不需要 thread-safe。",
            "演算法、reporter 或 log 丟例外時，controller 計數（<code>la_errors</code>、<code>reporter_errors</code>），並用安全的預設值：目前的 MCS，沒有 report。TX 與 RX 繼續執行。",
            "<code>set_mcs</code> 讓它進入 fixed mode。<code>set_la</code> 的 <code>auto</code> 把 MCS 交回演算法。"],
          input: "Feedback 與 RX 量測", output: "MCS 與 CONTROL payload",
          code: ["<code>la/controller.py</code>：<code>LinkController</code>"],
          opts: ["<code>--la-log PATH</code>"],
          gui: "<code>Link adaptation</code> tab：auto／fixed 與最近 20 個決定。"
        },
        {
          name: "BurstPolicy",
          does: ["TX worker 在每個 data burst 之前呼叫 BurstPolicy。",
            "<code>entry_for_next_burst</code> 問 <code>LinkAdaptation.decide()</code>，然後回傳 MCS。",
            "<code>control_plan</code> 決定 CONTROL payload：report 到期時是 FEEDBACK，否則是 LINK_HEADER。",
            "Node 閒置時，<code>idle_wait_s</code> 告訴 TX worker 何時醒來。Worker 最多睡 reporter 的 period（20 ms）。",
            "每個送出的 burst 經過 <code>on_sent</code> 交給 LinkAdaptation。"],
          input: "LA 的決定、到期的 report", output: "MCS 與 CONTROL payload",
          code: ["<code>tx_app.py</code>：<code>BurstPolicy</code>、<code>ControlPlan</code>", "<code>la/controller.py</code>：<code>entry_for_next_burst</code>、<code>control_plan</code>、<code>idle_wait_s</code>"],
          opts: ["—"],
          gui: "<code>TX MCS</code> card。"
        },
        {
          name: "ControlHandler",
          does: ["Receiver 把每個 CRC 正確的 CONTROL frame 交給 ControlHandler。",
            "src_node 等於自己時，handler 回傳 <code>DROP_BURST</code>：整個 burst 是自己的洩漏。",
            "dst_node 不是 0 也不是自己時，handler 忽略這個 frame。",
            "FEEDBACK 經過 <code>FeedbackTracker</code>（重複、peer 重新啟動）之後，交給 <code>LinkAdaptation.on_feedback()</code>。",
            "讀不懂的 LCP 不會讓 receiver 失敗。Handler 只計數。"],
          input: "CONTROL payload", output: "ACCEPT 或 DROP_BURST；feedback 給 LA",
          code: ["<code>link.py</code>：<code>ControlHandler</code>", "<code>la/controller.py</code>：<code>on_control</code>"],
          opts: ["—"],
          gui: "<code>Feedback age</code> card：小於 0.2 s 是綠色，小於 0.5 s 是黃色，其他是紅色。"
        },
        {
          name: "ReceiveObserver",
          does: ["RX worker 在每個 chunk 之後呼叫 ReceiveObserver。",
            "Observer 把 peer preamble 的 SNR、DATA burst reports 與 RX health 交給 FeedbackReporter。",
            "Receiver 已經丟棄自己的洩漏。所以它的 SNR 不進 report。",
            "Chunk 只有 control-only bursts 時，observer 用 <code>control_only=True</code> 呼叫 <code>on_preambles</code>。"],
          input: "一個 RX chunk 的結果", output: "Reporter 的輸入",
          code: ["<code>rx_app.py</code>：<code>ReceiveObserver</code>", "<code>la/controller.py</code>：<code>on_receive</code>"],
          opts: ["—"], gui: "—"
        }
      ]
    },
    {
      id: "n-la", cls: "ctl", row: "Control",
      name: "LinkAdaptation", sum: "LinkAdaptation 為每個 data burst 選 MCS。預設是 OLLA：inner loop 與 outer loop。",
      blocks: [
        {
          name: "可以替換的模組",
          does: ["LinkAdaptation 是一個 Protocol：<code>on_sent</code>、<code>on_feedback</code>、<code>decide</code>、<code>snapshot</code>。不需要 base class。",
            "<code>--link-adaptation</code> 選擇模組：<code>olla</code>（預設）、<code>fixed</code>、<code>arf</code>、<code>package.module:Class</code> 或 <code>path/file.py:Class</code>。",
            "<code>--link-adaptation-arg K=V</code> 把參數傳給 constructor。",
            "Ladder 來自 <code>configs/la/thresholds_pluto_v5.yaml</code>：MCS 8、9、10、12、13、15、16、17。MCS 14 不在 ladder 上：它與 MCS 15 一樣是每個 symbol 3 bits，但是需要的 SNR 高 1 dB。"],
          input: "Feedback reports", output: "<code>Decision(mcs_index, reason)</code>",
          code: ["<code>la/api.py</code>：<code>LinkAdaptation</code>、<code>LaContext</code>、<code>Decision</code>", "<code>la/loader.py</code>：<code>build_link_adaptation</code>", "<code>la/thresholds.py</code>：<code>build_ladder</code>"],
          opts: ["<code>--link-adaptation SPEC</code>（預設 olla）、<code>--link-adaptation-arg K=V</code>、<code>--mcs-index N</code>（起始的 MCS）"],
          gui: "<code>Link adaptation</code> tab：threshold 表。"
        },
        {
          name: "OLLA inner loop",
          does: ["Inner loop 把 filtered peer SNR 加上 offset，得到 SNR_eff。",
            "它選 threshold 不超過 SNR_eff 的最高 rung。",
            "升階需要新 rung 的 threshold + 1 dB、連續 2 份帶新 SNR 的 report，而且距離上次改變至少 0.1 s。降階立即執行。",
            "SNR filter 的時間常數是 0.1 s。連續 2 份新 report 比 filter 低 6 dB 以上時，filter 從 report 重新開始。"],
          input: "Peer 的 SNR", output: "MCS",
          code: ["<code>la/olla.py</code>：<code>OllaLinkAdaptation</code>"],
          opts: ["<code>hysteresis_up_db=1.0</code>、<code>up_hold_reports=2</code>、<code>min_dwell_s=0.1</code>、<code>snr_tau_s=0.1</code>、<code>snr_drop_db=6</code>、<code>snr_drop_reports=2</code>"],
          gui: "<code>SNR_eff / offset</code> card。60 秒 trend 的副軸是 filtered SNR。"
        },
        {
          name: "OLLA outer loop",
          does: ["Outer loop 依 peer 回報的 DATA subframes 改變 offset。",
            "每個收到的 subframe 讓 offset 加 0.00505 dB。每個遺失的 subframe 讓 offset 減 0.5 dB。所以 offset 停止漂移時，loss 是 1%。",
            "沒有 HARQ 救回遺失。所以 target 是 1%，不是 LTE 的 10%。",
            "一份 report 的更新乘上一個只由 report 大小決定的 gain。一份典型大小、全部遺失的 report 讓 offset 減 2 dB。",
            "Offset 限制在 −10 dB 到 +3 dB。RX overflow 計數改變的 report 不更新 offset。"],
          input: "Peer 的 ok 與 lost subframes", output: "Offset",
          code: ["<code>la/olla.py</code>：<code>_outer_update</code>"],
          opts: ["<code>target_loss=0.01</code>、<code>step_down_db=0.5</code>、<code>max_report_down_db=2.0</code>、<code>offset_min_db=-10</code>、<code>offset_max_db=3</code>"],
          gui: "<code>Peer-reported loss (10 s)</code> card。"
        },
        {
          name: "Stale 與 fixed mode",
          does: ["0.5 s 沒有收到 report 時，MCS 每 0.5 s 降一階，最低到 MCS 8。",
            "Reports 一直來，但是沒有一份聽到 0.5 s 前送出的 burst 時，forward link 已經斷了。MCS 也每 0.5 s 降一階。",
            "Fixed mode 時，OLLA 凍結 outer loop。回到 auto 時，它沿用原來的 offset。"],
          input: "時間與 reports", output: "較慢的 MCS",
          code: ["<code>la/olla.py</code>：<code>decide</code>、<code>on_mode</code>"],
          opts: ["<code>stale_after_s=0.5</code>、<code>stale_step_s=0.5</code>、<code>fallback_mcs=8</code>"],
          gui: "Trend 在 feedback stale 的時段塗紅。"
        }
      ]
    },
    {
      id: "n-reporter", cls: "ctl", row: "Control",
      name: "FeedbackReporter", sum: "FeedbackReporter 在 RX 端決定量什麼與何時回報。",
      blocks: [
        {
          name: "可以替換的模組",
          does: ["FeedbackReporter 是一個 Protocol：<code>on_preambles</code>、<code>on_burst</code>、<code>on_rx_health</code>、<code>due</code>、<code>next_due_s</code>、<code>build</code>。",
            "自訂的 reporter 用非 0 的 <code>reporter_id</code>。額外的量測放在 type 0x80–0xFF 的 TLV。"],
          input: "RX 量測", output: "<code>FeedbackReport</code>",
          code: ["<code>la/api.py</code>：<code>FeedbackReporter</code>、<code>FeedbackReport</code>", "<code>la/loader.py</code>：<code>build_reporter</code>"],
          opts: ["<code>--feedback-reporter SPEC</code>（預設 default）、<code>--feedback-reporter-arg K=V</code>"],
          gui: "—"
        },
        {
          name: "DefaultReporter",
          does: ["它量測窗內 peer preamble 的 SNR：mean、10th percentile 與 last。",
            "它累計 DATA ok 與 lost subframes，並記下最多 8 個最近的 DATA bursts。",
            "Report 在三種情況到期：有新的 DATA burst，而且距離上一份 20 ms；有 PSDU 的 FCS 失敗，而且距離上一份 20 ms；距離上一份 200 ms（keepalive）。",
            "Control-only burst 只更新 SNR。它不讓 report 到期。否則兩個閒置的 node 會一直互相回應。",
            "每次啟動，它抽一個隨機的 32-bit session id。"],
          input: "Preamble SNR、burst reports、RX health", output: "每 20 ms 到 200 ms 一份 report",
          code: ["<code>la/reporter.py</code>：<code>DefaultReporter</code>"],
          opts: ["<code>period_ms=20</code>、<code>keepalive_ms=200</code>、<code>max_entries=8</code>"],
          gui: "Peer 的 <code>Feedback age</code> card。"
        }
      ]
    }
  ];

  return { MODULES: MODULES, TOUR: TOUR, RUN: RUN, SYMPTOMS: SYMPTOMS, FILES: FILES, NODE_MODULES: NODE_MODULES };
})();

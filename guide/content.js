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
        { text: "Node 1 從 UDP port 52211 收 bytes。Node 2 把這些 bytes 送到 UDP port 52222。反方向用 52221 與 52212。" }
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
    ["control.py、la/", "（雙向 node）", "Control 與 feedback 的 wire format、可以替換的 link adaptation"],
    ["udp_send.py、udp_recv.py", "你的程式", "最小的 socket producer 與 consumer"],
    ["experiments/ota_udp_throughput/", "（量測工具）", "各 MCS 的 airtime 與 OTA UDP throughput sweep"],
    ["experiments/tx_v5/", "（量測工具）", "Wire v5 的錄音、goodput 與 acquisition 門檻量測"]
  ];

  return { MODULES: MODULES, TOUR: TOUR, RUN: RUN, SYMPTOMS: SYMPTOMS, FILES: FILES };
})();

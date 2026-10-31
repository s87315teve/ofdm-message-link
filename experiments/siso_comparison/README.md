# 與 SISO-master 的架構對照與 SNR 模擬

把這個專案的 PHY 和另一個 OFDM 專案 SISO-master（DVB-T 式設計：RS(204,188) + K=7 conv r1/2、
comb pilot、CP-based MNC 同步）放進同一組 channel 比較。完整的圖文報告在 [`index.html`](index.html)，
用瀏覽器直接開。

> 這是**模擬**，不是實測。沒有模擬相位雜訊、IQ imbalance、ADC 量化、AGC 與外部干擾，
> 實機的門檻會比這裡高幾 dB。

## 檔案

| 檔案 | 內容 |
|---|---|
| `sweep_ofdm_link.py` | 用 repo 裡的 `encode_burst` / `decode_burst` 掃 SNR，比較現在的 RX 與兩個只改 RX 的修正 |
| `siso_model.py` | SISO-master `lib/` 的 TX 鏈與 RX（MNC 同步、整數 CFO 搜尋、pilot 內插、hard Viterbi、RS 判斷）的 numpy 移植 |
| `siso_sweep.py` | 用 siso-rx 的串流迴圈收連續 symbol 並掃 SNR |
| `results/*.json` | `index.html` 使用的數據 |

## 重現

```bash
python setup.py build_ext --inplace       # 需要原生 Turbo decoder
PYTHONPATH=src python experiments/siso_comparison/sweep_ofdm_link.py 150 ofdm_link.json
python experiments/siso_comparison/siso_sweep.py experiments/siso_comparison 80 siso.json
```

## 結論摘要（AWGN，900 B burst，50% 成功點的真實 SNR）

| 接收端 | MCS 0 | MCS 4 |
|---|---:|---:|
| SISO（每個 188 B symbol） | 6.4 dB | — |
| 這個專案，現在的 RX | 7.4 dB | 13.0 dB |
| ＋ 傳入正確的 noise variance | 5.2 dB | 8.2 dB |
| ＋ 平滑 channel estimate | 3.5 dB | 6.4 dB |

- `link.py` 沒有把 `input_noise_variance` 傳給 `StreamingBurstDecoder`，`burst.py` 因此用 `1e-6`，
  LLR 全部被 `SOFT_LLR_CLIP = 64` 截斷，Turbo decoder 等於拿到 hard decision。
- LLR 修好之後，低 SNR 的失敗幾乎都是 burst header（96 bit × 3 重複，用單一 LS training symbol 等化）。
- RX 顯示的 SNR（decision-directed EVM）比真實 SNR 低 3–5 dB；SISO 印出的 ρ/(1−ρ) 與真實值差在 ±0.5 dB 內。

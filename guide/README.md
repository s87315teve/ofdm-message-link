# 單向 OFDM message link 的互動導覽

線上版：<https://s87315teve.github.io/ofdm-message-link/guide/>。

離線時用瀏覽器開啟 `index.html`。不需要 server，也不需要網路。

| 檔案 | 功能 |
|---|---|
| `index.html` | 頁面結構：四個分頁（工作流、Burst lab、程式結構、執行） |
| `style.css` | 版面與顏色（light 與 dark） |
| `content.js` | 全部的說明文字：模組、blocks、導覽步驟、指令、症狀、檔案對照 |
| `app.js` | 互動邏輯 |
| `data.js` | 自動產生：每種 MCS 與每個 fragment 大小的 payload OFDM symbols |
| `build_data.py` | 用這個範例的 encoder 產生 `data.js` |

PHY 改變後，請在 repository root 重新產生 `data.js`：

```bash
python -m guide.build_data
```

`index.html#lab`、`#code`、`#run` 可以直接開啟對應的分頁。

## 文字的寫法

說明文字用中文，並保留 ASD-STE100 的習慣：

- 一個句子只說一件事，句子要短。
- 使用主動語態：寫「這個 block 加上 header」，不寫「header 被加上」。
- 同一個東西只用同一個詞（例如固定寫 message，不混用「訊息」「封包」）。
- 專有名詞保留英文，並和程式裡的寫法相同（preamble、cyclic prefix、MCS、burst）。
- 英文用美式拼法（center、color、behavior）。
- 名詞的定義集中在 [`docs/00-prerequisites.md`](../docs/00-prerequisites.md) 的名詞表。改名詞時，README、
  `docs/`、`guide/` 與 `experiments/` 要一起改。

這份教學固定使用的寫法：

| 使用 | 不使用 |
|---|---|
| message（應用程式的一段資料） | 訊息、封包 |
| datagram（只指 UDP datagram） | 用 datagram 指 fragment |
| fragment、fragment header（28 B） | Datagram layer、datagram header |
| burst 遺失、遺失 burst | 掉包、丟包、掉 packet |
| burst 偵測 | acquisition |
| FEC 種類 | wire version（只在說明程式名稱時出現一次） |
| decode stages | decode funnel |
| 終端機 | terminal |
| 確認文字（`--acknowledgement` 的內容） | acknowledgement |
| 錯誤訊息（程式印出的文字） | 訊息 |

# OFDM message link 的互動導覽

線上版：<https://s87315teve.github.io/ofdm-message-link/guide/>。

離線時用瀏覽器開啟 `index.html`。不需要 server，也不需要網路。

| 檔案 | 功能 |
|---|---|
| `index.html` | 頁面結構：五個分頁（工作流、Packet lab、程式結構、執行、雙向 node）。兩張 block diagram 是 inline SVG |
| `style.css` | 版面與顏色（light 與 dark） |
| `content.js` | 全部的說明文字：模組、blocks、導覽步驟、指令、症狀、檔案對照，以及雙向 node 的模組（`NODE_MODULES`） |
| `app.js` | 互動邏輯 |
| `data.js` | 自動產生：每種 MCS 與每個 fragment 大小的 payload OFDM symbols |
| `build_data.py` | 用這個範例的 encoder 產生 `data.js` |

PHY 改變後，請在 repository root 重新產生 `data.js`：

```bash
python -m guide.build_data
```

`index.html#lab`、`#code`、`#run`、`#node` 可以直接開啟對應的分頁。

前四個分頁說明單向的 `tx_app` 與 `rx_app`。`雙向 node` 分頁說明 `node_app`：兩個 node、共用的 Pluto、
`LinkController` 的三個 hooks、link adaptation 與 feedback。`app.js` 的 `NODE_BLOCKS` 把這張圖的 blocks
對應到 `NODE_MODULES`。完整說明在 `docs/07-reference.md` 的 7.8 與 7.9 節。

## 文字的寫法

說明文字用中文，並保留 ASD-STE100 的習慣：

- 一個句子只說一件事，句子要短。
- 使用主動語態：寫「這個 block 加上 header」，不寫「header 被加上」。
- 同一個東西只用同一個詞（例如固定寫 message，不混用「訊息」「封包」）。
- 專有名詞保留英文，並和程式裡的寫法相同（preamble、cyclic prefix、MCS、burst）。

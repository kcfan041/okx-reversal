# okx-reversal

OKX USDT 永續合約「山寨幣暴漲後巨大 5m 紅 K → 反轉」假說的歷史資料庫與回測專案。

目前狀態：**規劃完成，尚未實作。**

## 文件閱讀順序

1. [`docs/idea.md`](docs/idea.md)：原始研究構想與假說
2. [`docs/PLAN.md`](docs/PLAN.md)：完整實作規格（照著開發）
3. [`docs/DECISIONS.md`](docs/DECISIONS.md)：每個設計決定與原因

## 一句話摘要

在 Windows 主機上以工作排程器每 5 分鐘把 OKX 所有 USDT 永續 5m K 線同步進本機 DuckDB，
再用回測程式統計「暴漲後第一根巨大紅 K」之後的報酬、MAE/MFE，並與隨機時間點比較。

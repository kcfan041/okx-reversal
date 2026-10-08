# okx-reversal

OKX USDT 永續合約「山寨幣暴漲後巨大 5m 紅 K → 反轉」假說的歷史資料庫與回測專案。

狀態：第一版已實作，排程同步運作中。

在 Windows 主機上以工作排程器每 5 分鐘把 OKX 所有 USDT 永續 5m K 線同步進本機 DuckDB，
再用回測程式統計「暴漲後第一根巨大紅 K」之後的報酬、MAE/MFE，並與隨機時間點比較。

## 文件

1. [`docs/idea.md`](docs/idea.md)：原始研究構想與假說
2. [`docs/PLAN.md`](docs/PLAN.md)：完整實作規格
3. [`docs/DECISIONS.md`](docs/DECISIONS.md)：每個設計決定與原因

## 部署（Windows 主機）

前置：安裝 [uv](https://docs.astral.sh/uv/)（`winget install astral-sh.uv`）與 Git。

```powershell
git clone https://github.com/kcfan041/okx-reversal
cd okx-reversal
uv sync                                   # 下載 Python 3.12、建立 .venv、依 uv.lock 安裝

# 小型驗證（約 1 分鐘）
uv run python -m okxrev.sync --smoke
uv run python -m okxrev.sync --status
uv run pytest

# 安裝排程（以系統管理員身分開啟 PowerShell）
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

排程：`okx-reversal-sync`，每 5 分鐘、單次最多 4 分鐘、低優先權、不論是否登入都執行，
直接呼叫 `.venv\Scripts\pythonw.exe -m okxrev.sync`。首次回補一年約需 1.5～2 天。

## 日常操作

```powershell
uv run python -m okxrev.sync --status          # 資料涵蓋狀態、回補進度、缺口
Get-Content logs\sync.log -Tail 20 -Wait         # 同步日誌

Start-ScheduledTask   -TaskName okx-reversal-sync   # 立即跑一次
Disable-ScheduledTask -TaskName okx-reversal-sync   # 暫停
Enable-ScheduledTask  -TaskName okx-reversal-sync   # 恢復
powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1   # 移除（保留資料）
```

## 回測

```powershell
uv run python -m okxrev.backtest                      # 三種模式 + 隨機基準 + 圖
uv run python -m okxrev.backtest --sweep --no-charts  # 加跑參數掃描
uv run python -m okxrev.backtest --since 2026-01-01 --until 2026-06-30
uv run python -m okxrev.charts --mode pump_red --limit 50   # 依 events.csv 重畫圖
```

輸出：`results/events.csv`、`results/summary.json`、`results/sweep.csv`、`results/charts/*.png`。
所有門檻都在 `config.yaml`。資料尚未回補完成時也能執行，`summary.json` 的 `data_coverage` 會記錄涵蓋範圍。

backtest 以唯讀方式開資料庫；剛好碰到 sync 寫入時會自動等待重試。

## 更新程式

```powershell
Disable-ScheduledTask -TaskName okx-reversal-sync
git pull
uv sync
Enable-ScheduledTask -TaskName okx-reversal-sync
```

## 結構

```text
okxrev/
  config.py      設定讀取與路徑解析
  log.py         日誌（logs/*.log，5 MB × 5 輪替）
  okx_client.py  OKX REST：限速、重試、錯誤分類
  db.py          DuckDB 建表、upsert、鎖等待
  sync.py        同步 CLI（forward + 分層 backfill + --status）
  detector.py    Pump、巨大紅 K、cooldown
  metrics.py     forward return、MAE/MFE、新高、統計、bootstrap
  backtest.py    回測 CLI
  charts.py      Event Replay 圖
scripts/         Windows 排程安裝／移除
tests/           pytest（含防偷看未來資料測試）
```

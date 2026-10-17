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
直接呼叫 `.venv\Scripts\pythonw.exe -m okxrev.sync`。首次回補一年約需 10 小時。

## 日常操作

```powershell
uv run python -m okxrev.sync --status          # 資料涵蓋狀態、回補進度、缺口
Get-Content logs\sync.log -Tail 20 -Wait         # 同步日誌

Start-ScheduledTask   -TaskName okx-reversal-sync   # 立即跑一次
Disable-ScheduledTask -TaskName okx-reversal-sync   # 暫停
Enable-ScheduledTask  -TaskName okx-reversal-sync   # 恢復
powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1   # 移除（保留資料）
```

## 網頁監控台

```powershell
uv run python -m okxrev.web            # 開啟 http://127.0.0.1:8765/（自動開瀏覽器，Ctrl+C 結束）
uv run python -m okxrev.web --port 9000 --no-browser
```

只綁本機 127.0.0.1，需要時手動開，關掉不影響排程。分頁：

- **儀表板**：資料庫大小、回補各層進度、延遲、最近執行紀錄、錯誤、資料缺口
- **同步控制**：立即執行、暫停／恢復、只同步指定合約、即時日誌
- **回測**：從網頁執行（日期區間、參數掃描、事件圖），即時日誌，結果與隨機基準比較表
- **事件**：篩選排序 `events.csv`，點選看詳細與事件圖，可跳到 K 線
- **K 線**：任選合約與時間的互動 K 線、成交額、均量、BTC、事件標記

暫停 = 建立 `data/PAUSE` 旗標檔：排程仍會觸發但立即結束（不需系統管理員權限）。
命令列等效：`New-Item data\PAUSE` / `Remove-Item data\PAUSE`；暫停中可用 `--force` 強制執行一次。

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

## 個人戰績與操作檢討

1. 在 OKX 建立 **唯讀** API key（只勾「讀取」，不要交易／提幣），在專案根目錄建立 `.env`（已在 `.gitignore`）：

   ```text
   OKX_API_KEY=...
   OKX_API_SECRET=...
   OKX_API_PASSPHRASE=...
   ```

2. 執行：

   ```powershell
   uv run python -m okxrev.review --sync --replicate   # 同步帳戶 → 操作診斷 → 全市場可複製性掃描（約 2 分鐘）
   uv run python -m okxrev.account --status            # 只看同步狀態與總覽
   ```

   或在網頁「戰績」分頁按「同步並分析」。

- 合約（SWAP/FUTURES）讀 `positions-history`；現貨讀 `fills-history` 以加權平均成本自行計算損益（非 USDT/USDC 計價者不計價）
- OKX API 只提供近 3 個月；資料累積在 `data/account.duckdb`，定期同步即可保留更久
- 輸出：`results/trades_enriched.csv`（每筆交易＋進場特徵、MAE/MFE、出場後走勢、行為標記）、`results/review.json`（問題清單、反事實試算）、`results/replicate.json|csv`（可複製性）
- 可複製性：從你獲利的交易找出「進場當下可觀察」的條件組合，掃全部合約歷史，以你的中位數持倉時間與停損模擬，與隨機基準比較；樣本外（你交易期間以外）≥ 30 次且 CI95 下界 > 0 才判定「可複製」

## 過熱監控

網頁「過熱監控」分頁，或使用命令列：

```powershell
uv run python -m okxrev.heatdata --backfill   # 補主動買賣量與合約溢價歷史（約 2024-09 起，可中斷續跑）
uv run python -m okxrev.heat --snapshot       # 目前全市場過熱排行
uv run python -m okxrev.heat --coin CT-USDT-SWAP
uv run python -m okxrev.heat --evaluate       # 找門檻＋盲測，輸出 results/heat.json
```

- 過熱分數分五大類：價格偏離、量能與買盤、槓桿與擁擠、合約溢價、情緒與爆倉。每項指標換算成「這個幣自己過去 30 天的百分位」，分數為各類平均。情緒類尚無歷史資料，只供參考，不計入分數。
- 每次同步會順便更新主動買賣量和標記／指數價格（每小時）；關注清單和過熱前 10 名的多空比與爆倉資料每 5 分鐘更新。
- 紙上交易每小時更新一次排行，並對關注清單與前 10 名發出提醒（過熱、降溫、rule03/04 觸發），只顯示在網頁，不下單。
- 多空比與爆倉：關注清單與前 10 名每 5 分鐘；其他所有幣爆倉每 30 分鐘、多空比每 6 小時（`sentiment_all`、`liq_all_every_min`、`ls_all_every_min`），讓情緒類早點累積歷史。
- 驗證（`--evaluate`）另外輸出風險表：24h 做空在止損 10%／20% 下的成績、6h＋止損 10%、24 小時內最大反向漲幅（MAE）分位數、逆漲 ≥20%／≥50% 的比例、資金費（只有 2026-06 之後）。
- 紙上交易員 `heat80_*`、`heat90_*`：過熱分數第一次 ≥80／≥90（前 24 小時都未達到）時做空，出場 24h、24h＋止損 10%、24h＋止損 20%（`paper`、`paper_thresholds`、`paper_exits`）。同時進入即時紙上交易與舊資料盲測重播，與 rule03/04 同一排行比較。
- 急漲回落做空 `fade_pump`（`okxrev/fade.py`）：前 30 天跌 ≥20%，24 小時內從低點漲 ≥50%，5 分 K 收盤回吐漲幅 25% 後，下一根開盤做空；止盈 12%、止損 50%、最長 7 天，同幣 72 小時冷卻。對照組 `fade_pump_sl30`（止損 30%）、`fade_pump_anypre`（不看前 30 天）。帳本 A 依每筆風險 2% 下單（權益 × 2% ÷ 止損），帳本 B（20x）不適用。觸發時在過熱監控提醒列出進場、止盈、止損與建議逐倉部位（用唯讀金鑰查 OKX 權益，查不到用 `paper.fade.equity_fallback`）。開關在設定面板 `paper.kinds.fade`、`paper.fade.*`。
- AI 討論的「目前證據」由 `results/heat.json`、`results/discover.json` 與盲測重播結果自動產生，重跑驗證後自動更新。
- 設定在 `config.yaml` 的 `heat:` 區塊（watchlist、alert_threshold、alert_cooldown_h、top_n）。

## AI 討論

網頁「AI 討論」分頁：選一個合約，用聊天的方式問問題。AI（`.env` 設定的 LLM，預設 9router 的 `claude`）會自己呼叫 18 個**唯讀**工具查資料再回答：

| 類別 | 工具 |
|---|---|
| 行情 | `price_summary`（任意時段 K 線摘要，含 2024-09 起的舊資料）、`live_market`（即時價、掛單簿、資金費率、OI）、`market_list` |
| 指標 | `indicators`（規則／模型用的全部特徵）、`heat`、`heat_validation`、`derivatives`（資金費率、OI、主動買賣、溢價、多空比、爆倉） |
| 策略 | `rules_status`、`rule_history`、`pump_events`、`paper_trading`（即時／盲測）、`similar_situations`、`simulate_trade` |
| 你的帳戶 | `my_trades`、`trading_review`、`bots`、`positions`（唯讀 key） |
| 其他 | `project_overview` |

- 每次提問上限：12 回合、40 次工具、6 萬字元工具結果（`config.yaml` 的 `chat:`）。達到上限時 AI 會先根據已有資料回答，並出現「繼續深入查詢」按鈕。每日工具呼叫上限 500 次。
- 對話、工具呼叫紀錄存在 `data/chat.duckdb`。點回答裡的 UTC 時間可以直接開 K 線；點工具標籤可以看 AI 拿到的原始資料。
- 對話內容（包含你的帳戶與持倉數字）會送到 `.env` 設定的 LLM 服務。
- 另有 6 個工具：`fine_candles`（1s/1m K 線＋微結構）、`recent_trades`（最近 500 筆成交）、`analyze_event`、`find_similar_events`、`test_template`、`custom_rules`（見下方事件實驗室）。AI 不能動用盲測期。

## 事件實驗室

網頁「事件實驗室」分頁：把一次「急漲後爆跌」拆開來看，找歷史上類似的案例，變成規則並驗證，再持續觀察。

1. **盯盤**（上方）：選幣，切 1 秒／1 分 K（即時模式每 2／10 秒更新），附 VWAP、最近 1/5/15 分鐘量價、逐筆成交、爆倉；有持倉時畫出進場／止損／強平價。「標成事件」用畫面中央時間拆解事件，「問 AI」直接帶到 AI 討論。
2. **① 事件清單**：最近 24 小時全市場急漲（≥15%）後從高點跌 ≥8% 的幣；也可以從 K 線分頁「送到事件實驗室」、過熱監控、AI 回答中的時間（Shift+點）帶入。
3. **② 細看事件**：起漲、高點、觸發點、崩跌低點；漲跌幅與速度、量比、BTC、上市天數、1m／1s 細節、資金費率／OI／爆倉。
4. **③ 樣板**：系統依事件自動產生條件（漲幅、上漲時間、從高點跌幅、高點後時間、量比…），每項有滑桿，調整後立刻重算歷史成績。觸發點只用當下已知資料（測試涵蓋）。
5. **④ 找類似事件**：全市場 2 年 5 分 K 依形狀距離排序，前 200 筆抓 1m 確認（存在 `data/fine.duckdb`，下次不用重抓）。不含盲測期。
6. **⑤ 結果**：做空／做多 × 6 種出場（15m、1h、6h、24h、6h／24h＋2×ATR 止損），扣手續費與滑價，依日平均、與同期隨機比較；主資料前 2/3 為找參數期、後 1/3 為驗證期；依成交額、BTC 環境分組；每月。
7. **盲測**：2024-10～2025-09 舊資料，每個樣板只算一次（之後固定顯示），所有盲測次數都有紀錄（`results/lab/blind_log.json`）。用越多次越不可信。
8. **⑥ 存成規則**：存到 `data/custom_rules.json`，自動成為紙上交易交易員 `lab_xxxxxxxx`（標示判定與是否已盲測），也會納入舊資料盲測重播。每輪紙上交易（5 分 K）觸發後再用 1m 確認，確認的會出現在過熱監控的提醒（類型 lab）。

```powershell
uv run python -m okxrev.events_lab --build-index                                # 建立索引（約 2 分鐘，results/lab/index.parquet）
uv run python -m okxrev.events_lab --similar CT-USDT-SWAP@2026-10-13T02:50       # 拆解＋找類似＋1m 確認（約 6 分鐘）
```

- 索引只收「從低點漲 ≥15% 且從高點跌 ≥5%」的 5 分 K，所以樣板最小值就是這兩個數字。新資料進來後需要「建立／更新索引」才會納入（介面超過 24 小時會提醒）。
- 1 秒 K 只有約 7 個月（新幣從上市起），只用來細看，不用來建規則；1 分 K 約 2 年。
- 已下架的幣沒有資料，有存活者偏差。設定在 `config.yaml` 的 `lab:`。

## 設定面板

網頁右上角 ⚙（或各分頁標題旁的小 ⚙）打開右側設定面板，不用切換頁面：資料收集範圍與頻率、關注清單與提醒、紙上交易員類別與帳本、AI 預設模型與上限、背景程式優先權，以及金鑰／LLM 是否已設定（只顯示有沒有）。

- 設定存在 `data/settings.json`，覆蓋 `config.yaml` 的同名值；可單項或整區「回預設」。不會修改 `config.yaml` 與 `.env`。
- 研究參數（偵測門檻、切分、盲測期間、手續費）不在面板裡，改了會讓既有驗證失效。

## 暫停／恢復資料收集

網頁右上角「資料收集」按鈕或「同步控制」分頁可以暫停、恢復；也可以用命令列：

```powershell
uv run python -m okxrev.collect --pause     # 暫停：排程仍每 5 分鐘觸發但立即結束；紙上交易、過熱更新、提醒一起停
uv run python -m okxrev.collect --resume
uv run python -m okxrev.collect --status
```

- 不會修改 Windows 工作排程本身（需要系統管理員權限）。若排程被手動停用，狀態會提示要以系統管理員身分執行 `Enable-ScheduledTask -TaskName okx-reversal-sync`。

- 恢復後下一次同步會補回 K 線、主動買賣量、溢價、資金費率；多空比（OKX 只留約 1 天）、爆倉（每幣最新約 100 筆）、OI 5 分鐘（幾小時）超過保留期的部分無法補回，介面會列出。
- 暫停中「立即執行同步」「立即跑一輪」會先確認；網頁的即時查詢（持倉、盯盤、AI 即時資料）不受影響。

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
  account.py     帳戶同步（簽名 API）、交易正規化、戰績統計
  features.py    進場特徵與可組合的情境條件
  review.py      操作診斷 CLI（進場／出場／風控／行為、反事實）
  replicate.py   獲利情境全市場回測、樣本內外比較
  fine.py        1s/1m K 線與逐筆成交（即時抓取、1m 快取）、微結構指標
  events_lab.py  事件實驗室：形狀、樣板、全市場索引、相似事件、分期與盲測
  custom_rules.py 實驗室規則存檔、紙上交易員、1m 確認提醒
  web/           網頁監控台（FastAPI + 單頁 HTML，lightweight-charts）
scripts/         Windows 排程安裝／移除
tests/           pytest（含防偷看未來資料測試）
```

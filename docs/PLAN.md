# okx-reversal 實作規格 v1

> 本文件是第一版的完整開發規格。研究動機見 [`idea.md`](idea.md)，各項取捨原因見 [`DECISIONS.md`](DECISIONS.md)。
> 所有時間一律使用 **UTC**，資料庫內時間戳為 **毫秒整數（Unix ms）**。

---

## 0. 目錄

1. 目標與範圍
2. 執行環境
3. 專案結構
4. 設定檔 `config.yaml`
5. 資料來源：OKX REST API
6. 資料庫：DuckDB
7. 同步程式 `okxrev.sync`
8. Windows 工作排程器
9. 事件偵測 `okxrev.detector`
10. 指標計算 `okxrev.metrics`
11. 回測 `okxrev.backtest`
12. 事件圖 `okxrev.charts`
13. 防止偷看未來資料（look-ahead）檢查清單
14. 已知限制
15. 資源估算
16. 開發順序與驗收標準
17. 部署步驟（Windows 主機）
18. 第二版以後

---

## 1. 目標與範圍

### 要回答的問題

> 暴漲後出現第一根異常巨大 5m 紅 K，是否具有預測後續下跌的能力？而且這個能力是否明顯優於隨機？

### 第一版要做

- 把 OKX **所有 USDT 本位永續合約**（`*-USDT-SWAP`）的 **5m K 線**持續同步進本機 DuckDB。
  - 往回補 **1 年**歷史。
  - 之後每 5 分鐘自動補最新資料。
- 在 Windows 主機上以工作排程器 24/7 執行，低資源占用。
- 偵測事件：Pump → 第一根巨大紅 K。
- 計算事件後報酬、MAE、MFE、是否再創新高、BTC 環境分組。
- 三種條件組合 + 參數掃描 + 隨機基準對照。
- 輸出 `events.csv`、`summary.json`、終端摘要、每個事件一張 Event Replay 圖。

### 第一版不做

Open Interest、Funding Rate、清算資料、Order Book、lower high／收復判斷、即時 Scanner、自動交易、通知。

---

## 2. 執行環境

| 項目 | 規格 |
|---|---|
| 正式執行機 | Windows 主機，幾乎 24/7 開機 |
| Python | 3.12（由 uv 管理，`.python-version`） |
| 套件管理 | [uv](https://docs.astral.sh/uv/)：`pyproject.toml` + `uv.lock` |
| 虛擬環境 | 專案內 `.venv`（`uv sync` 建立） |
| 開發機 | 任意（macOS / Windows 皆可），程式必須跨平台 |

### 依賴（`pyproject.toml`）

```text
pandas>=2.2
numpy>=1.26
pyarrow>=15
duckdb>=1.1
requests>=2.31
pyyaml>=6.0
matplotlib>=3.8
filelock>=3.13
```

開發依賴（`[dependency-groups] dev`）：`pytest`。`uv.lock` 進 git，確保各機器版本一致。

不使用 Polars、mplfinance、ccxt，以減少依賴。

注意：uv 管理的 Python 安裝在使用者目錄（Windows：`%APPDATA%\uv\python\`），`.venv` 依賴它；
不要 `uv python uninstall` 這個版本，否則排程會失效。

### 跨平台規則

- 路徑一律使用 `pathlib.Path`，以 repo 根目錄為基準解析 `config.yaml` 中的相對路徑。
- 不使用 `fcntl`、`signal.SIGALRM` 等 Unix 專用功能。
- 檔案讀寫一律指定 `encoding="utf-8"`。
- 終端輸出避免依賴特殊字元，Windows 主控台需能正常顯示（可在程式開頭 `sys.stdout.reconfigure(encoding="utf-8")`）。

---

## 3. 專案結構

```text
okx-reversal/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── config.yaml
├── .gitignore
├── .gitattributes
├── docs/
│   ├── idea.md
│   ├── PLAN.md
│   └── DECISIONS.md
├── okxrev/
│   ├── __init__.py
│   ├── config.py          # 讀取 config.yaml，提供型別化設定物件與路徑解析
│   ├── log.py             # logging 設定（RotatingFileHandler + console）
│   ├── okx_client.py      # REST 呼叫、限速、重試、錯誤分類
│   ├── db.py              # DuckDB 連線、建表、upsert、查詢
│   ├── sync.py            # 同步主程式（CLI）
│   ├── detector.py        # Pump、巨大紅 K、事件產生
│   ├── metrics.py         # forward return、MAE/MFE、新高、BTC 環境
│   ├── backtest.py        # 回測主程式（CLI）
│   └── charts.py          # Event Replay 圖（CLI 也可單獨執行）
├── scripts/
│   ├── install_task.ps1   # 註冊 Windows 工作排程
│   └── uninstall_task.ps1 # 移除 Windows 工作排程
├── data/                  # git 忽略：okx.duckdb
├── logs/                  # git 忽略：sync.log
└── results/
    ├── events.csv         # 進 git
    ├── summary.json       # 進 git
    ├── sweep.csv          # 進 git
    └── charts/            # git 忽略
```

---

## 4. 設定檔 `config.yaml`

所有門檻與路徑都放在這裡，程式內不寫死。以下為第一版預設值：

```yaml
paths:
  db: data/okx.duckdb
  logs: logs
  results: results
  lock: data/sync.lock

okx:
  base_url: https://www.okx.com
  proxy: null                 # 例如 http://127.0.0.1:7890
  timeout_sec: 15
  max_requests_per_sec: 5     # OKX 上限 20 次/2 秒，取一半
  max_retries: 5
  backoff_base_sec: 1.0       # 指數退避：1, 2, 4, 8, 16 秒

universe:
  inst_type: SWAP
  quote_suffix: -USDT-SWAP
  btc_inst: BTC-USDT-SWAP
  exclude: []                 # 手動排除的合約
  bar: 5m

sync:
  run_budget_sec: 240         # 單次執行最多 4 分鐘，之後自行結束
  forward_first: true         # 先補最新資料，再用剩餘時間回補
  backfill_start: "2025-10-01T00:00:00Z"
  backfill_layers_days: [30, 90, 180, 365]   # 分層回補，見 7.4
  smoke_insts: [BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP]
  smoke_days: 1

detector:
  vol_ma_bars: 20             # 成交量均量視窗（不含當根）
  pump:
    ret_24h_min: 0.50         # 24h 漲幅 > 50%
    ret_6h_min: 0.30          # 或 6h 漲幅 > 30%
    vol_ratio_min: 4.0        # pump 視窗內至少一根量能 >= 均量 × 4
    vol_lookback_bars: 72     # 檢查量能的視窗：事件前 6h
    measure: close_to_close   # close_to_close | low_to_close，見 9.2
    allow_new_listing: true   # 上市不足 24h 的幣，見 9.2
    new_listing_min_bars: 72  # 至少 6h 資料才判斷
  red_candle:
    ret_max: -0.08            # 單根報酬 <= -8%
    vol_ratio_min: 4.0
    ret_basis: prev_close     # prev_close | open，見 9.3
  cooldown_bars: 288          # 同一合約事件後 24h 內不再產生事件

metrics:
  entry: next_open            # next_open | event_close
  horizons:                   # 名稱: K 線根數
    30m: 6
    1h: 12
    6h: 72
    24h: 288
    3d: 864
  excursion_bars: 288         # MAE/MFE/新高 視窗：24h
  big_drop_threshold: -0.20   # 「24h 跌超過 20%」統計
  fee_per_side: 0.0005        # taker 0.05%
  slippage_per_side: 0.0005

btc_regime:
  ret_4h_weak: -0.01          # BTC 4h 報酬 <= -1% → weak
  ret_4h_strong: 0.01         # BTC 4h 報酬 >= +1% → strong
  ema_bars: 288               # 另外記錄 BTC 是否在 24h EMA（5m 288 根）之下

backtest:
  since: null                 # 例如 "2025-10-01"
  until: null
  sweep:
    red_ret_max: [-0.05, -0.08, -0.10, -0.15]
    red_vol_ratio_min: [3.0, 4.0, 6.0]
  baseline:
    random_samples: 5000
    seed: 42
  bootstrap:
    iterations: 2000
    seed: 42

charts:
  enabled: true
  max_charts: 300
  pre_hours: 6
  post_hours: 48
  dpi: 110
```

`config.py` 需驗證必要欄位存在，並把 `paths.*` 解析成以 repo 根目錄為基準的絕對路徑。

---

## 5. 資料來源：OKX REST API

不需要 API Key，全部是公開端點。

### 5.1 合約清單

```
GET /api/v5/public/instruments?instType=SWAP
```

- 篩選 `instId` 結尾為 `-USDT-SWAP`，且 `state == "live"`。
- 會用到的欄位：`instId`、`uly`、`ctVal`、`listTime`（ms）、`expTime`、`state`。
- 截至 2026-10 約有 **485 檔**。

### 5.2 最新 K 線（日常更新用）

```
GET /api/v5/market/candles?instId={id}&bar=5m&limit=300
```

- 回傳最新資料，最多 300 根，最早只能回到約 1440 根以前。
- 日常更新每檔只需 1 次請求。

### 5.3 歷史 K 線（回補用）

```
GET /api/v5/market/history-candles?instId={id}&bar=5m&limit=100&after={ts}
```

- `after={ts}`：回傳**早於** `ts` 的資料（往過去翻頁）。
- `before={ts}`：回傳**晚於** `ts` 的資料。
- 每次最多 100 根。
- 回傳空陣列 → 已到上市以前，該合約回補完成。

### 5.4 K 線欄位

回傳每列為字串陣列，**時間由新到舊**：

```
[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
```

| 欄位 | 意義 | 存入 |
|---|---|---|
| ts | K 線開始時間（ms） | `ts` |
| o h l c | 開高低收 | `open high low close` |
| vol | 張數 | `vol` |
| volCcy | 幣數量 | `vol_ccy` |
| volCcyQuote | 計價幣（USDT）成交額 | `vol_quote` |
| confirm | `1` = 已收盤，`0` = 未完成 | 只存 `confirm == "1"` |

**分析一律使用 `vol_quote`（USDT 成交額）**，不同幣之間才可比較。

### 5.5 限速與錯誤處理（`okx_client.py`）

- 全域 token bucket，每秒最多 `max_requests_per_sec` 次。
- 每次請求帶 `timeout`，使用 `requests.Session` 重用連線。
- 錯誤分類：

| 情況 | 處理 |
|---|---|
| HTTP 429，或回傳 code `50011`（請求過於頻繁） | 指數退避後重試 |
| HTTP 5xx、連線錯誤、逾時 | 指數退避後重試，最多 `max_retries` 次 |
| 回傳 code `51001`（合約不存在） | 不重試，回傳特定例外，由 sync 標記為下架 |
| 其他非 0 code | 記錄日誌，跳過該合約，本次執行繼續處理下一檔 |

- 所有字串數值轉成 float／int。

---

## 6. 資料庫：DuckDB

單一檔案 `data/okx.duckdb`。

### 6.1 資料表

```sql
CREATE TABLE IF NOT EXISTS candles (
    inst_id    VARCHAR NOT NULL,
    ts         BIGINT  NOT NULL,       -- K 線開始時間，UTC ms
    open       DOUBLE  NOT NULL,
    high       DOUBLE  NOT NULL,
    low        DOUBLE  NOT NULL,
    close      DOUBLE  NOT NULL,
    vol        DOUBLE,
    vol_ccy    DOUBLE,
    vol_quote  DOUBLE,
    PRIMARY KEY (inst_id, ts)
);

CREATE TABLE IF NOT EXISTS instruments (
    inst_id      VARCHAR PRIMARY KEY,
    uly          VARCHAR,
    ct_val       DOUBLE,
    list_time    BIGINT,               -- OKX 提供的上市時間
    state        VARCHAR,              -- live | delisted
    first_seen   BIGINT,               -- 本系統第一次在清單看到
    last_seen    BIGINT,               -- 本系統最後一次在清單看到
    delisted_at  BIGINT                -- 本系統判定下架的時間
);

CREATE TABLE IF NOT EXISTS sync_state (
    inst_id        VARCHAR PRIMARY KEY,
    oldest_ts      BIGINT,             -- 已有資料最早一根
    newest_ts      BIGINT,             -- 已有資料最新一根
    backfill_done  BOOLEAN DEFAULT FALSE,  -- 已到 backfill_start 或上市時間
    last_forward_at  BIGINT,
    last_backfill_at BIGINT,
    last_error     VARCHAR
);
```

### 6.2 寫入規則

- 一律使用 `INSERT OR REPLACE INTO candles ...`（依主鍵去重，重複執行不會產生重複資料）。
- 批次寫入：把 API 結果轉成 pandas DataFrame，再以 `con.register()` + `INSERT OR REPLACE ... SELECT` 一次寫入。
- 每批寫入與 `sync_state` 更新放在**同一個 transaction**，避免中斷後狀態不一致。

### 6.3 併發規則（重要）

DuckDB 同一時間只允許「一個讀寫程序」**或**「多個唯讀程序」，兩者不能同時存在。

- **sync**：先向 API 抓一批資料（不開資料庫），抓完才開讀寫連線、寫入、立刻關閉。每次持有連線的時間應在 1 秒內。
  實作：累積到 3 萬列或 20 秒就寫一次；每次執行另在 `sync_runs` 表記錄一列摘要（供 `--status` 顯示）。
- **backtest / charts / --status**：以 `read_only=True` 開啟；若被鎖，等待 2 秒重試，最多 30 次。
- **sync 遇到資料庫被唯讀程序占用**：同樣等待重試；超過 60 秒仍無法寫入，就保留這批資料到下一輪，並寫日誌，不視為錯誤。
- backtest 的資料讀取方式：**每次只讀一個合約**到 pandas，算完就釋放；不可一次把全表讀進記憶體（全表約 5,000 萬列）。
- 若之後發現衝突太頻繁，可改成 backtest 先複製一份 `okx.duckdb` 快照再讀（第二版考慮）。

---

## 7. 同步程式 `okxrev.sync`

### 7.1 CLI

```text
python -m okxrev.sync                 # 一般執行（排程呼叫這個）
python -m okxrev.sync --status        # 顯示資料涵蓋狀態，不連網
python -m okxrev.sync --smoke         # 小型驗證：smoke_insts、smoke_days
python -m okxrev.sync --max-minutes N # 覆寫 run_budget_sec
python -m okxrev.sync --no-backfill   # 只補最新資料
python -m okxrev.sync --only BTC-USDT-SWAP ETH-USDT-SWAP
```

### 7.2 單次執行流程

```text
取得檔案鎖（filelock，timeout=0；取不到就記日誌後結束，exit code 0）
  ↓
1. 更新合約清單
     - 呼叫 instruments
     - 新出現的合約：寫入 instruments、建立 sync_state
     - 清單中消失的合約：state = delisted、delisted_at = now
  ↓
2. 補最新資料（forward）
     - 對象：所有 state = live 的合約，BTC 排第一
     - 每檔呼叫 /market/candles 一次
     - 若 newest_ts 距今超過 300 根（例如主機關機很久），
       改用 history-candles 往後翻頁補到最新
       （實測：只給 before 會回傳「最新」100 根，必須同時給 after = before + 101 根 形成視窗）
     - 寫入後更新 newest_ts
  ↓
3. 回補歷史（backfill），直到用完 run_budget_sec
     - 依 7.4 分層順序挑選下一個要補的合約
     - 每檔每次最多翻 N 頁（建議 20 頁 = 2000 根），再換下一檔，避免單檔獨占
     - 呼叫 history-candles，after = oldest_ts
     - 回傳空 → backfill_done = true
     - oldest_ts <= backfill_start → backfill_done = true
  ↓
釋放鎖，寫一行執行摘要到日誌：
  耗時、請求數、寫入列數、forward 檔數、backfill 進度、錯誤數
```

### 7.3 時間預算

- 從啟動開始計時；每次發請求前檢查是否超過 `run_budget_sec`，超過就收尾。
- forward 預估：485 次請求 ÷ 每秒 5 次 ≈ 100 秒。
- 剩下約 140 秒用於 backfill（約 700 次請求 = 7 萬根）。
- 首次執行（資料庫為空）時 forward 也會很快，因為沒有需要補的缺口。

### 7.4 分層回補順序

目的：資料只補了一部分時，回測也能涵蓋全部合約，而不是只有少數幾檔有完整一年。

```text
第 1 層：所有合約補到「距今 30 天」
第 2 層：所有合約補到「距今 90 天」
第 3 層：所有合約補到「距今 180 天」
第 4 層：所有合約補到 backfill_start（約 365 天）
```

- 同一層內：BTC-USDT-SWAP 永遠第一，其餘依 `oldest_ts` 由新到舊排序（最缺資料的先補）。
- 上市時間晚於該層目標的合約，補到上市即視為該層完成。

### 7.5 下架合約

- 合約從清單消失：標記 `delisted`，保留所有資料，不再 forward。
- 若 backfill 期間遇到 `51001`：同樣標記 `delisted`、`backfill_done = true`。
- 這樣**從系統開始運作以後**才下架的幣，資料會被保存下來。

### 7.6 `--status` 輸出

```text
DB: data/okx.duckdb  (812.4 MB)
Instruments: 485 live, 3 delisted
Candles: 41,233,812 rows
Backfill: layer 3/4, 402/488 done
Latest bar lag: max 7 min (BTC-USDT-SWAP 0 min)
Last run: 2026-10-09 03:15 UTC, 238 s, 1,204 requests, 0 errors

Oldest coverage (top 10 least covered):
inst_id              oldest            newest            bars     done
XXX-USDT-SWAP        2026-09-30 12:00  2026-10-09 03:15  2,500    no
...
```

另外要列出**資料缺口**：同一合約相鄰兩根時間差大於 5 分鐘的段數與總長度（OKX 偶有停機）。

### 7.7 日誌

- `logs/sync.log`，`RotatingFileHandler`，單檔 5 MB，保留 5 份。
- 層級 INFO；請求層級的細節用 DEBUG。
- 以 `pythonw.exe` 執行時沒有主控台，所以日誌是唯一的觀察管道。

---

## 8. Windows 工作排程器

### 8.1 排程設定

| 設定 | 值 |
|---|---|
| 工作名稱 | `okx-reversal-sync` |
| 執行程式 | `<repo>\.venv\Scripts\pythonw.exe` |
| 參數 | `-m okxrev.sync` |
| 起始位置 | `<repo>` |
| 觸發 | 立即開始，每 5 分鐘重複，無限期 |
| 已有執行中 | `IgnoreNew`（不啟動新的） |
| 執行時間上限 | 30 分鐘 |
| 錯過排程 | `StartWhenAvailable`（盡快補跑） |
| 優先權 | 7（低於一般） |
| 電源 | 使用電池時也執行、不因切換電池而停止 |
| 登入類型 | `S4U`（不論是否登入都執行，不需儲存密碼） |
| 權限 | 一般權限（`Limited`），不需系統管理員執行程式 |

### 8.2 `scripts/install_task.ps1`（行為規格）

- 以腳本所在位置推算 repo 根目錄。
- 檢查 `.venv\Scripts\pythonw.exe` 存在，否則提示先建立 venv 並結束。
- 以 `New-ScheduledTaskAction`、`New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)`、`New-ScheduledTaskSettingsSet`、`New-ScheduledTaskPrincipal -LogonType S4U -RunLevel Limited` 組合後 `Register-ScheduledTask -Force`。
- 優先權：`$settings.Priority = 7`。
- 完成後顯示：工作名稱、下次執行時間、查看日誌與移除的方法。
- 註冊排程本身可能需要以系統管理員身分開啟 PowerShell；腳本需偵測並給出清楚訊息。

### 8.3 `scripts/uninstall_task.ps1`

- `Unregister-ScheduledTask -TaskName okx-reversal-sync -Confirm:$false`。
- 不刪除資料與日誌。

### 8.4 手動控制

```powershell
Start-ScheduledTask   -TaskName okx-reversal-sync   # 立即跑一次
Disable-ScheduledTask -TaskName okx-reversal-sync   # 暫停
Enable-ScheduledTask  -TaskName okx-reversal-sync   # 恢復
Get-ScheduledTaskInfo -TaskName okx-reversal-sync   # 上次結果、下次時間
```

---

## 9. 事件偵測 `okxrev.detector`

輸入：單一合約的 5m DataFrame（依 `ts` 排序），加上同時間軸的 BTC DataFrame。
輸出：事件列表。

### 9.1 前處理

- 以 `ts` 重新建立連續 5 分鐘索引；缺的 K 線保留為 NaN，**不補值**。
- 衍生欄位（以下的 `t` 為事件 K 棒的索引）：
  - `ret_1`：`close[t] / close[t-1] - 1`
  - `body_ret`：`close[t] / open[t] - 1`
  - `vol_ma`：`vol_quote[t-20 .. t-1]` 的平均（**不含當根**，以 `shift(1).rolling(20)` 計算）
  - `vol_ratio`：`vol_quote[t] / vol_ma[t]`

### 9.2 Pump 條件（以事件前一根 `t-1` 收盤為準）

```text
ret_24h_pre = close[t-1] / close[t-1-288] - 1
ret_6h_pre  = close[t-1] / close[t-1-72]  - 1

pump = (ret_24h_pre > ret_24h_min OR ret_6h_pre > ret_6h_min)
       AND max(vol_ratio[t-72 .. t-1]) >= pump.vol_ratio_min
```

- `measure = low_to_close`：把分母改成對應視窗內的最低 `low`，可以捕捉「先跌後暴漲」的型態。第一版預設 `close_to_close`，回測時可切換比較。
- **新上市合約**：若可用歷史不足 288 根，但至少有 `new_listing_min_bars` 根，且 `allow_new_listing = true`，則以「第一根的 open」當分母計算 `ret_24h_pre`，並在事件上標記 `is_new_listing = true`。
- 視窗內有缺值時，該根不判斷（不產生事件）。

### 9.3 巨大紅 K 條件（事件 K 棒 `t`）

```text
red_ret = ret_1[t]      (ret_basis = prev_close)
        或 body_ret[t]  (ret_basis = open)

red = red_ret <= red_candle.ret_max
      AND vol_ratio[t] >= red_candle.vol_ratio_min
```

`events.csv` 兩種報酬都要記錄。

### 9.4 事件產生

- 三種模式，供 11.1 的條件組合使用：
  - `red_only`：只需 `red`
  - `pump_red`：`pump AND red`
  - `pump_red_btcweak`：`pump AND red AND btc_regime == weak`
- **「第一根」規則**：依時間掃描，產生事件後的 `cooldown_bars` 根內，同一合約不再產生事件。
- 每個事件記錄 `event_ts = ts[t]`（事件 K 棒的開始時間）。事件在 `t` **收盤**時才確認，即 `ts[t] + 5 分鐘`。

---

## 10. 指標計算 `okxrev.metrics`

### 10.1 進場價

- `next_open`（預設）：`entry = open[t+1]`
- `event_close`：`entry = close[t]`
- 若 `t+1` 不存在或缺值，事件標記 `incomplete`，不納入統計。

### 10.2 Forward return

對每個 horizon `h`（根數）：

```text
fwd_ret_{name} = close[t+h] / entry - 1
```

- 負值代表下跌，即**對做空有利**。
- 資料不足（例如事件太接近現在）：該欄為 NaN；統計時各 horizon 分開計算有效樣本數。

### 10.3 MAE / MFE（以做空角度定義，視窗 `excursion_bars`）

```text
window = [t+1 .. t+excursion_bars]
MAE = max(high[window]) / entry - 1     # 做空最大不利，>= 0 代表先往上跑多少
MFE = min(low[window])  / entry - 1     # 做空最大有利，<= 0
```

另外記錄：
- `mae_before_mfe`：MAE 是否發生在 MFE 之前（判斷「先被掃再跌」）。
- `bars_to_mfe`：到達 MFE 的根數。

### 10.4 是否再創新高

```text
pre_high = max(high[t-288 .. t])     # 事件前 24h 最高（含事件 K 棒）
new_high_24h = max(high[t+1 .. t+288]) > pre_high
```

### 10.5 BTC 環境（在 `t` 收盤時可得的資訊）

```text
btc_ret_1h = btc_close[t] / btc_close[t-12] - 1
btc_ret_4h = btc_close[t] / btc_close[t-48] - 1
btc_below_ema = btc_close[t] < EMA(btc_close, span=ema_bars)[t]

btc_regime = weak    if btc_ret_4h <= ret_4h_weak
           = strong  if btc_ret_4h >= ret_4h_strong
           = neutral otherwise
```

BTC 在 `t` 缺值時，用 `t` 之前最近的一根（`ffill`，最多 3 根），超過就標記 `unknown`。

### 10.6 交易成本

```text
short_pnl_net_{name} = -fwd_ret_{name} - 2 × (fee_per_side + slippage_per_side)
```

第一版**不計入** Funding Rate（記在已知限制）。

---

## 11. 回測 `okxrev.backtest`

### 11.1 CLI

```text
python -m okxrev.backtest                  # 用 config 預設值，三種模式
python -m okxrev.backtest --sweep          # 加跑參數掃描
python -m okxrev.backtest --no-charts
python -m okxrev.backtest --since 2026-01-01 --until 2026-06-30
python -m okxrev.backtest --insts BTC-USDT-SWAP PEPE-USDT-SWAP
```

### 11.2 流程

```text
讀取 BTC 全部資料，計算 BTC 環境欄位
  ↓
對每個合約（排除 BTC 本身）：
  讀取單一合約資料 → 前處理 → 合併 BTC 欄位
  → 依三種模式產生事件 → 計算指標 → 累積到事件表
  → 同時抽樣隨機基準點（見 11.4）
  ↓
統計（見 11.3）
  ↓
輸出 events.csv、summary.json、（--sweep 時）sweep.csv
  ↓
印出終端摘要
  ↓
若 charts.enabled：為 pump_red 模式的事件畫圖（最多 max_charts 張）
```

### 11.3 統計項目（每種模式、每個 horizon）

| 項目 | 說明 |
|---|---|
| `n` | 有效樣本數 |
| `n_insts` | 涉及的不同合約數 |
| `n_days` | 涉及的不同日期數（檢查是否集中在少數幾天） |
| `mean`、`median`、`std` | forward return |
| `bear_rate` | forward return < 0 的比例 |
| `big_drop_rate` | 24h forward return <= `big_drop_threshold` 的比例 |
| `new_high_rate` | 24h 內再創新高的比例 |
| `mae_mean`、`mae_median`、`mae_p90` | |
| `mfe_mean`、`mfe_median` | |
| `short_pnl_net_mean` | 扣成本後的做空平均報酬 |
| `mean_ci95` | bootstrap 95% 信賴區間 |
| `by_btc_regime` | 上述主要項目依 strong / neutral / weak / unknown 分組 |
| `by_month` | 每月事件數與 24h 平均報酬（檢查穩定性） |

### 11.4 隨機基準

不比較隨機就無法判斷是否為 edge。產生兩組基準：

1. **`random_any`**：從所有合約、所有有完整前後資料的 K 棒中均勻抽樣 `random_samples` 個時間點，用同樣的方式計算指標。
2. **`random_pump`**：只從「pump = true 但 red = false」的 K 棒中抽樣。用來回答：**巨大紅 K 本身有沒有額外資訊**，還是暴漲後本來就容易跌。

輸出中每個模式都要並列：

```text
              n     24h mean   24h median   bear_rate   MAE mean   CI95 of mean
pump_red     184    -14.2%     -11.0%       68.6%       +11.4%     [-17.9%, -10.6%]
random_pump  5000    -3.1%      -1.8%       55.2%        +9.8%     [ -3.5%,  -2.7%]
random_any   5000    -0.2%      -0.1%       50.4%        +2.1%     [ -0.3%,  -0.1%]
```

### 11.5 參數掃描（`--sweep`）

- 針對 `pump_red` 模式，跑 `red_ret_max × red_vol_ratio_min`（預設 4 × 3 = 12 組）。
- 每組一列寫入 `sweep.csv`：參數、`n`、24h mean/median、bear_rate、MAE mean、short_pnl_net_mean、CI95。
- **注意多重比較**：12 組裡挑最好的一組必然偏樂觀。報告中要並列全部結果，不只顯示最佳。

### 11.6 `events.csv` 欄位

```text
mode, inst_id, event_ts, event_time_utc, is_new_listing,
ret_1h_pre, ret_6h_pre, ret_24h_pre, pump_vol_ratio_max,
red_ret_prev_close, red_ret_body, red_vol_ratio,
btc_ret_1h, btc_ret_4h, btc_below_ema, btc_regime,
entry_price,
fwd_ret_30m, fwd_ret_1h, fwd_ret_6h, fwd_ret_24h, fwd_ret_3d,
mae_24h, mfe_24h, mae_before_mfe, bars_to_mfe, new_high_24h,
short_pnl_net_24h, incomplete
```

### 11.7 `summary.json`

```json
{
  "generated_at": "...",
  "data_coverage": {
    "n_insts": 485, "first_ts": "...", "last_ts": "...",
    "backfill_layer": 3, "delisted_included": 3
  },
  "config": { "...": "本次使用的 detector / metrics 設定" },
  "modes": {
    "red_only": { "...": "11.3 的統計" },
    "pump_red": { },
    "pump_red_btcweak": { }
  },
  "baselines": {
    "random_any": { },
    "random_pump": { }
  },
  "caveats": ["survivorship bias: delisted before system start are missing", "..."]
}
```

---

## 12. 事件圖 `okxrev.charts`

- 只用 matplotlib，K 線自行以矩形和線段繪製。
- 每個事件一張 PNG：`results/charts/{inst_id}_{YYYYMMDD_HHMM}.png`。
- 時間範圍：事件前 `pre_hours`（6h）到事件後 `post_hours`（48h）。
- 三個子圖（共用 X 軸）：
  1. 價格 K 線
     - 垂直線標示事件 K 棒。
     - 水平線標示 entry、MAE 價位、MFE 價位。
     - 標題：合約、時間、red_ret、vol_ratio、24h 報酬、btc_regime。
  2. 成交額（`vol_quote`）長條圖與 20 根均量線
  3. BTC 收盤價，以事件時間為基準正規化為 100
- 圖片下方以文字列出：pre 6h/24h 漲幅、fwd 30m/1h/6h/24h、MAE、MFE、是否再創新高。
- `python -m okxrev.charts --events results/events.csv --mode pump_red --limit 50` 可單獨重畫。
- 畫完每張就 `plt.close(fig)`，避免記憶體累積。

---

## 13. 防止偷看未來資料檢查清單

開發時逐項確認，並寫成單元測試：

- [ ] `vol_ma` 使用 `shift(1)`，不包含當根。
- [ ] Pump 條件只用 `t-1` 以前的收盤。
- [ ] 紅 K 條件只用 `t` 本身與更早的資料。
- [ ] BTC 環境只用 `t` 收盤以前的資料。
- [ ] 進場價在 `t` 收盤之後（`open[t+1]`）。
- [ ] Cooldown 依時間順序處理，不能用整段資料一次決定。
- [ ] 隨機基準與事件使用完全相同的指標函式。
- [ ] 參數掃描結果全部列出，不只顯示最佳。

建議單元測試：用人工造的小 DataFrame（例如 400 根），在已知位置放一根 -10% 大量紅 K，驗證事件位置、entry、fwd return、MAE/MFE 計算正確；再把未來的價格改掉，確認事件偵測結果不變。

---

## 14. 已知限制

1. **倖存者偏差**：OKX REST 查不到已下架合約（實測 SRM、OMG、WAVES、ANT、FTT 都回傳 `51001`）。系統開始運作前就下架的幣不會出現在資料中；這些幣通常是崩最慘的，所以下跌幅度可能被低估。系統開始運作後才下架的幣會保留。
2. **新幣資料較短**：上市不足一年的合約只有上市後的資料。
3. **資料缺口**：OKX 偶有停機或 K 線缺失；缺值不補，只在 `--status` 回報，並讓相關事件標記 `incomplete`。
4. **未計 Funding Rate**：暴漲幣常有高正 Funding，做空會收到 Funding，所以實際結果可能略好於回測。第二版加入。
5. **成交量只看 OKX**：不代表全市場。
6. **事件集中**：大盤崩跌時許多幣同時觸發，事件並不獨立。用 `n_days` 和 `by_month` 檢查，第二版可考慮依日期分群的 bootstrap。
7. **雲端排程不可行**：GitHub Actions 機器在美國，OKX 會封鎖美國 IP。
8. **流動性**：小幣在巨大紅 K 後的實際成交價可能比 `open[t+1]` 差很多；`slippage_per_side` 只是粗略估計。

---

## 15. 資源估算

| 項目 | 估計 |
|---|---|
| 合約數 | 約 485 |
| 一年 5m K 線 | 105,120 根/檔 → 全部約 5,100 萬列 |
| 首次回補請求數 | 約 51 萬次（每次 100 根） |
| 首次回補時間 | 以每秒 5 次、每 5 分鐘可用約 140 秒計算，約 1.5～2 天完成 |
| 日常請求 | 每 5 分鐘約 485 次；每天約 14 萬次，網路流量約 50 MB |
| CPU | 單核不到 5%，每次執行約 2～4 分鐘後結束 |
| 記憶體 | sync 約 100～200 MB；backtest 逐檔讀取，約 500 MB 以內 |
| 硬碟 | DuckDB 第一年約 1 GB，之後每年增加約 1 GB |

---

## 16. 開發順序與驗收標準

| # | 工作 | 驗收標準 |
|---|---|---|
| 1 | `config.py`、`log.py`、`pyproject.toml`、`config.yaml` | 讀取設定成功，路徑正確解析為絕對路徑；日誌寫入 `logs/` |
| 2 | `okx_client.py` | 能取得合約清單（約 485 檔）、BTC 最新 300 根、BTC 歷史 100 根；人為加快速率時能觸發並正確處理 429 |
| 3 | `db.py` | 建表成功；同一批資料寫入兩次，列數不變；讀寫連線與唯讀連線互相等待時不會崩潰 |
| 4 | `sync.py` + `--smoke` | `--smoke` 約 1 分鐘內完成；`--status` 顯示 4 檔各約 288 根；中途 Ctrl+C 後再執行能接續、不重複 |
| 5 | `sync.py` 完整流程 | 時間預算到了會自己結束；分層回補順序正確；下架標記正確 |
| 6 | `install_task.ps1` / `uninstall_task.ps1` | Windows 上註冊成功，每 5 分鐘執行一次；日誌持續增加；登出後仍執行；移除後不再執行 |
| 7 | `detector.py` + 單元測試 | 第 13 節的單元測試通過 |
| 8 | `metrics.py` + 單元測試 | 手算的 fwd return、MAE、MFE、BTC regime 與程式一致 |
| 9 | `backtest.py` | 資料只補一部分時也能跑完；輸出三個檔案；summary 含資料涵蓋範圍；隨機基準存在 |
| 10 | `charts.py` | 隨機抽 10 張圖人工檢查，事件標記位置正確 |
| 11 | README | 照 README 在一台乾淨的 Windows 機器上能從零完成部署 |

測試框架：`pytest`（列在 `pyproject.toml` 的 `dev` group，不放進正式依賴）。執行：`uv run pytest`。

---

## 17. 部署步驟（Windows 主機）

```powershell
# 0. 前置：安裝 uv（winget install astral-sh.uv）、Git for Windows（或 gh 並 gh auth login）

git clone https://github.com/kcfan041/okx-reversal
cd okx-reversal
uv sync                      # 自動下載 Python 3.12、建立 .venv、依 uv.lock 安裝

# 1. 小型驗證（約 1 分鐘）
uv run python -m okxrev.sync --smoke
uv run python -m okxrev.sync --status
uv run python -m okxrev.backtest --no-charts

# 2. 正常的話，安裝排程（以系統管理員身分開啟 PowerShell）
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1

# 3. 觀察
Get-Content logs\sync.log -Tail 20 -Wait
uv run python -m okxrev.sync --status

# 4. 回補完成後（約 1.5～2 天）
uv run python -m okxrev.backtest --sweep
```

排程直接呼叫 `.venv\Scripts\pythonw.exe`，不經過 uv。

更新程式：

```powershell
Disable-ScheduledTask -TaskName okx-reversal-sync
git pull
uv sync
Enable-ScheduledTask -TaskName okx-reversal-sync
```

---

## 18. 第二版以後

依第一版結果決定優先順序：

1. **Funding Rate、Open Interest**：`/api/v5/public/funding-rate-history`、`/api/v5/rubik/stat/contracts/open-interest-volume`。
2. **收復／lower high 判斷**：紅 K 後 N 根內是否收回紅 K 開盤價、是否形成較低高點後跌破局部低點（idea.md 第 5、11 節第四階段）。
3. **補下架幣**：調查 OKX 歷史資料下載頁（okx.com/historical-data）的每日檔案是否包含已下架合約。
4. **依日期分群的 bootstrap**，處理事件不獨立的問題。
5. **即時 Scanner**：WebSocket 訂閱、Reversal Score、通知（不自動下單）。
6. **搬到非美國地區 VPS**：直接複製 `okx.duckdb` 與 repo 即可。

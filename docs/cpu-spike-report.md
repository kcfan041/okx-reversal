# CPU 週期性滿載調查報告

- 日期：2026-10-12
- 狀態：**已修正**（見第 9 節）

## 1. 問題描述

電腦 CPU 會週期性衝到 100%，一段時間後又自己降回來。發生時遊戲（EVE Online）和錄影（Overwolf OBS）可能會卡頓。

## 2. 結論

元兇是本專案的排程工作 `okx-reversal-sync`。它每 5 分鐘執行一次 sync，sync 一結束就會自動啟動一次 **paper trading tick**（`python -m okxrev.paper --tick`）。

這個 tick 每次都會把全部幣種最近 12 天的 5 分 K 重新讀出來，再從頭算一遍所有特徵。每次大約花 45–50 秒，期間 `pythonw.exe` 會吃掉整台電腦約 70–80% 的 CPU。

## 3. 調查過程與證據

### 3.1 系統層監測

用 Windows 效能計數器 `\Processor(_Total)` 和 `\Process(*)\% Processor Time`，每秒取樣一次，連續監測 10 分鐘（21:20:34 起，共 529 個樣本）。只記錄總 CPU 超過 70% 的時間點。

| 時間（UTC+8） | 總 CPU | 前幾名程序（佔整機 CPU 比例） |
|---|---|---|
| 21:20:47 | 100% | pythonw 75%、exefile 9%、ow-obs 3%、chrome 3% |
| 21:20:50 | 100% | pythonw 69%、exefile 12%、ow-obs 5% |
| 21:20:54 | 100% | pythonw 73%、exefile 8%、ow-obs 4% |
| 21:20:57 | 100% | pythonw 79%、exefile 9%、ow-obs 3% |
| …… | …… | 期間 pythonw 一直維持在 67–79% |

沒有滿載的時候，總 CPU 大約在 26–31%：

- `exefile.exe`（EVE Online，`E:\EVE\tq\bin64`，簽章有效）：10–20%
- `ow-obs.exe`（Overwolf 錄影）：約 4%

這兩個是穩定的背景負載，不是造成滿載的原因。

### 3.2 排程工作

| 項目 | 值 |
|---|---|
| 工作名稱 | `okx-reversal-sync` |
| 執行內容 | `H:\git\okx-reversal\.venv\Scripts\pythonw.exe -m okxrev.sync` |
| 觸發頻率 | 每 5 分鐘（`PT5M`） |
| 最近一次結果 | 0（成功） |

### 3.3 log 時間軸（log 時間是 UTC，加 8 小時就是本地時間）

`logs/sync.log`：

```
13:18:30 sync start (budget 240s)
13:18:33 forward: 485 instruments
13:19:13 backfill layer 1/4 ~ 4/4 complete
13:20:33 run done: 122.4s, 690 requests, 970 rows, forward 485 in 40s, errors 0, ok
```

`logs/paper.log`：

```
13:21:20 paper tick: {... 'sec': 36.9, 'total_sec': 47.2, 'n_traders': 37}
```

把兩份 log 對照起來：

- **13:18:30–13:20:33**：sync 執行。這段主要是在等網路請求（690 個 API 請求），CPU 用得不多。
- **13:20:33**：sync 結束，啟動 paper tick。
- **13:20:33–13:21:20**（本地 21:20:33–21:21:20）：paper tick 執行中，剛好就是監測到 `pythonw` 把 CPU 衝滿的那段時間。

最近幾次 tick 的耗時都很穩定，`total_sec` 都在 45–49 秒，所以每 5 分鐘都會重複出現一次滿載：

| 結束時間（UTC） | sec | total_sec |
|---|---|---|
| 13:01:13 | 38.0 | 47.3 |
| 13:06:13 | 37.1 | 46.5 |
| 13:11:13 | 36.1 | 45.0 |
| 13:16:18 | 39.5 | 49.4 |
| 13:21:20 | 38.3 | 46.5 |
| 13:26:20 | 38.3 | 48.0 |

## 4. 根本原因（程式碼層面）

1. `okxrev/sync.py` 的 `run_sync()` 在 `finally` 區塊裡呼叫 `spawn_paper(cfg)`（約第 373 行）。所以每次 sync 結束都會用 `pythonw` 另外啟動一個獨立的 `okxrev.paper --tick` 程序。這個程序用預設優先權執行，也沒有限制執行緒數。
2. `okxrev/paper.py` 的 `run_tick()` 每次都會呼叫 `load_market(cfg, since)`（約第 192 行），`since` 是現在往前 `window_days`（預設 12 天）。`load_market()` 做了這些事：
   - 用一條 SQL 從 DuckDB 讀出所有幣種、12 天的 5 分 K。以 485 個幣種來算，大約是 485 × 3456 ≈ 168 萬列。
   - 計算 `btc_features` 和 `market_breadth_recent`。
   - 對每個幣種逐一做 `prepare()`、`features()`、`attach()`（併入資金費率與未平倉量）。
3. 每次 tick 實際上只有 1 根新的 K 線要處理，但目前的做法是把 12 天的特徵全部從頭重算一遍。重複的計算佔了絕大部分的 CPU 時間，大約 37 秒。
4. pandas、numpy 和 DuckDB 會用多個核心，所以這段時間整台電腦的 CPU 幾乎被佔滿。

## 5. 已排除的可能

| 對象 | 理由 |
|---|---|
| EVE Online（`exefile.exe`） | 一直穩定在 10–20%，不會突然飆高；簽章有效（Fenris Creations） |
| Overwolf OBS（`ow-obs.exe`） | 一直穩定在約 4% |
| sync 本身的 API 抓取階段 | 主要在等網路；滿載時間點和 sync 結束、paper tick 開始的時間吻合 |
| BlueStacksHelper 排程 | 每 6 小時才執行一次，頻率對不上 |
| 其他 Windows 排程工作 | 最近 2 小時的執行時間都沒有和滿載時間吻合 |

## 6. 改善方案

| # | 方案 | 做法 | 效果 | 成本與風險 |
|---|---|---|---|---|
| 1 | 降低優先權 | `spawn_paper()` 的 `creationflags` 加上 `subprocess.BELOW_NORMAL_PRIORITY_CLASS`（或 `IDLE_PRIORITY_CLASS`）；排程本身也可以把優先權調低 | 前景的遊戲和錄影會優先拿到 CPU，比較不會卡頓 | 只改一行，風險極低。總計算量不變，CPU 看起來還是會滿載 |
| 2 | 限制執行緒數 | 啟動 paper 時設定 `OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 環境變數；DuckDB 已經在 `db.py` 設成 `threads: 2` | 尖峰的核心使用量下降 | 每次 tick 會跑比較久，但必須在 5 分鐘內跑完 |
| 3 | 縮小重算範圍 | 調低 `paper.window_days`，前提是特徵需要的回看長度足夠（目前 `len(raw) < 900` 的門檻大約是 3.1 天） | 計算量大約和天數成正比下降 | 要確認特徵值和原本一致，例如滾動視窗和分位數類的特徵 |
| 4 | 增量計算 | 快取上一次 tick 算好的 `feats`（例如存成 parquet），之後只讀取並計算新的 K 線和必要的回看段 | 效果最好，有機會從約 40 秒降到幾秒 | 要改計算架構，並且需要回歸測試確認結果和全量重算一致 |
| 5 | 降低頻率 | paper tick 改成每 10–15 分鐘一次，或是和 sync 拆開排程 | 滿載次數減少 | 訊號反應會變慢；`max_catchup_bars` 要足夠 |

建議順序：

- **短期**：先做方案 1 加方案 2。改動小，可以立刻消除對遊戲的影響。
- **中期**：做方案 4。這是從根本解決問題。方案 3 可以當作過渡。

## 7. 修正後的驗證方式

1. 在 PowerShell 每秒取樣一次，至少監測 10 分鐘，這樣能涵蓋 2 次 tick。記錄總 CPU 超過 70% 的時間點和 `pythonw` 的佔用比例：

   ```powershell
   Get-Counter '\Processor(_Total)\% Processor Time','\Process(pythonw*)\% Processor Time' -SampleInterval 1 -MaxSamples 600
   ```

2. 看 `logs/paper.log` 裡的 `sec` 和 `total_sec`，並和本報告的基準（約 37 秒和約 47 秒）比較。
3. 如果採用方案 3 或 4，要對同一個時間點比較新舊兩種算法產生的 `feats`、訊號和成交，確認結果一致。

## 8. 附註

- 使用者主觀感覺「每次大約 10 秒」，實際量到的高負載大約持續 45–50 秒。可能是只有其中一部分時間明顯感受得到卡頓。
- 本次調查只有讀取系統資訊和 log，沒有修改任何程式或設定。

## 9. 修正紀錄（2026-10-12）

### 9.1 補充發現：實際的大頭不是特徵重算

修正前把一次 tick 拆開實測（12 天視窗、37 個交易員）：

| 階段 | 耗時 | 說明 |
|---|---|---|
| `load_market`（讀 K 線＋算特徵） | 11.0 s | 本報告第 4 節指出的全量重算 |
| 機器學習／分身交易員 | **27.3 s** | 每個幣、每個方向各呼叫一次 LightGBM，約 2,900 次；`ml.matrix` 逐欄插入 DataFrame 很慢，而且 LightGBM 預設會用上全部 32 核 |
| LLM 審核員 | 8.7 s | 包裝的 rule01、rule02、ml 被重算一次 |
| 19 條規則 | 6.9 s | 每條規則對每個幣重新把整份特徵表轉成陣列 |

### 9.2 採用的方案

| 本報告方案 | 做法 | 檔案 |
|---|---|---|
| 1 降低優先權 | 新增 `background_popen()`，固定用 `IDLE_PRIORITY_CLASS`（可在 `config.yaml` 的 `background.priority` 改成 below_normal）。sync 後的 paper tick、每週自動重訓，以及網頁啟動的回測、探索、訓練、紙上交易、重播、戰績分析，全部改用它 | `okxrev/web/tasks.py`、`sync.py`、`paper.py` |
| 2 限制執行緒數 | 背景程序設定 `OMP/MKL/OPENBLAS/NUMEXPR_NUM_THREADS` 與 `OKXREV_THREADS` 為 2（`background.threads`）；LightGBM 的預測和訓練都讀 `OKXREV_THREADS`；重播的子程序各用 1 執行緒 | `tasks.py`、`ml.py`、`replay.py` |
| 3 縮小重算範圍 | `paper.window_days` 從 12 改成 8（最長特徵 7 日 = 2,016 根，加上補漏 36 根和冷卻 72 根，共 2,124 根，8 天有 2,304 根） | `config.yaml` |
| （新增）批次預測 | `ModelTrader.signals_batch`：所有幣的視窗合併成一張表，每個模型每個方向只預測一次；`ml.matrix` 改為一次組裝 | `traders.py`、`ml.py` |
| （新增）共用計算 | `Context.window()` 讓 19 條規則共用同一份特徵陣列；LLM 審核員直接沿用被包裝交易員在本輪已算好的訊號 | `paper.py`、`traders.py` |
| （新增）特徵組裝 | `discover.features` 改為先收集欄位再一次建立 DataFrame，結果與原本逐欄寫法逐位元相同，速度約 2 倍 | `discover.py` |
| 5 降低頻率 | 不需要，未採用 | – |
| 4 增量計算 | 修正後 tick 已降到約 9 秒，暫不需要 | – |

另外加了一則警告：tick 總耗時超過 240 秒時寫入 `paper.log`，用來發現 IDLE 優先權被前景程式長時間擠壓的情況。

### 9.3 正確性驗證

- **訊號完全相同**：改動前用同一個市場快照（12 天視窗、最近 36 根 K 線）存下 110 筆訊號作為基準（rule01、rule06、隨機組、twin、twin_you）；改動後重算，110 筆逐筆相同（交易員、合約、時間、方向、出場、分數到 1e-12）。
- **8 天與 12 天一致**：485 個合約最近 36 根的全部特徵，最大相對差異 `dist_ema` 1.8e-8（指數均線的暖機差異），其餘欄位小於 1e-12，沒有缺值差異。
- **新增測試**（`tests/test_cpu.py`）：
  - 背景程序的優先權旗標與執行緒環境變數設定正確。
  - LightGBM 執行緒預算有生效。
  - 批次預測與逐幣預測的訊號和機率一致（誤差 < 1e-12）。
  - 特徵陣列快取確實共用。
  - LLM 審核員不會重算內層交易員，且改名時不會汙染原始訊號。
  - 新的特徵組裝和原本 DataFrame 寫法逐位元相同。
- 全部 123 個測試通過。

### 9.4 修正後實測

| 指標 | 修正前 | 修正後 |
|---|---|---|
| paper tick `sec`（交易員計算） | 約 37 s | **2.4–2.9 s** |
| paper tick `total_sec` | 45–49 s | **8.5–12.8 s**（連續 6 次） |
| tick 程序優先權 | Normal | **Idle**（實測 `PriorityClass=Idle`） |
| tick 程序 CPU 峰值 | 整機 67–79% | **約 2 個核心＝整機 6.3%**（平均 1.1 核） |
| 10 分鐘監測，總 CPU 最大值 | 100% | **42.4%** |
| 10 分鐘監測，總 CPU > 70% 的秒數 | 每次 tick 約 45 秒 | **0** |
| 10 分鐘監測，總 CPU 平均 | – | 22.2%（主要是 EVE 和錄影） |

監測方式：`Get-Counter '\Processor(_Total)\% Processor Time'` 每秒取樣，連續 600 秒（本地 22:25:49 起），期間涵蓋 2 次 sync 加 paper tick。單一 tick 的程序 CPU 另以 `TotalProcessorTime` 每 0.5 秒取樣。

附註：Windows 上 venv 的 `pythonw.exe` 是啟動器，實際的 Python 直譯器是它的子程序。子程序會繼承 IDLE 優先權，所以監測時要看子程序，不能只看啟動器。

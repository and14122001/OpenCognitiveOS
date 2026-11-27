# 工程驗收矩陣

| 範圍 | 核對項目 | 本輪證據與限制 |
|---|---|---|
| PR #4 | 7ab15cb，63 項 | pr4.log |
| R3/R4 | 118 項，113 通過，5 跳過 | recovery.log。從 d929527 依序套用 consolidated、R4、CAS、F25、F20 partial、F21 registration，每層 git apply --check。PR #4 重疊檔按最終內容整合。歷史稽核用重現腳本保留於原始交付物。 |
| R4 Manifest | ZIP、consolidated、recovery log、R4 patch、原始測試日誌雜湊相符 | 兩份 Google 文件目前匯出的文字與原 Markdown 宣告的 SHA256 不同。原始 Markdown 位元組待取得。 |
| S21–S30 | 固定 Git SHA、工作區、epoch、CAS、Manifest capability 與舊入口拒絕已測 | OS 測試的五個 skip 仍保留；獨立 CI job 驗證 Linux 核心原語。Run 45978475746 的 15 組矩陣與 Linux 核心測試均成功。封存規格及 Manifest 採兩把控制面 HMAC 金鑰，回歸含錯誤金鑰、交易中斷、成功與封存後 CAS 遺失。正式 runner 整合 Pending。 |
| S31–S48 | 地端模型與雲端供應商 | ACP v1 初始化與 SQLite 零成本配額已做本地協定、雙任務共池與重放測試。真實 adapter、個別帳戶與費用證明待驗收。 |
| S49–S54、S61–S69 | 任務依賴、三層/每層三個子任務、父子步數預留 | 資料庫單交易執行；訊息、動態拓撲及推理資源治理待驗收。 |
| S55–S60、S70–S72 | SQLite 還原前驗證，無效來源保留目標 | 跨設備全量復原、配對基準、升級回滾及七天耐久待驗收。 |

本機回歸命令為 `PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`。

| 新增元件 | 可重跑證據 | 驗收邊界 |
|---|---|---|
| 本地訊息及路由 | test_task_messages.py | SQLite 持久化、兄弟任務範圍、epoch、順序、撤銷、TTL、每群組 64 筆上限。 |
| 備份與復原 | test_sealed_completion_rolls_back_and_replays | SQLite 與 Manifest 引用的 CAS 物件依外部固定摘要還原。控制器金鑰由操作者另管。 |
| 基準比較 | test_benchmark.py、compare-benchmark | 依預註冊 SHA、30 個以上任務、重複種子、同模型同預算比較。合成資料只驗證計算。 |
| Hermes ACP | github.com/NousResearch/hermes-agent，MIT，8fc0374447313e276f6b9a2764bfc750190c966a | CI 獨立測試 initialize；無模型推理。 |
| OpenCode ACP | github.com/anomalyco/opencode，MIT，f4e363c8295fa90a03dddc451882fffe8fbce357 | 固定 v1.19.6 Linux release SHA256，CI 獨立測試 initialize。 |
| Pi RPC | github.com/earendil-works/pi，MIT，ef4a1cfcca62b77ad3558337ff3e1c7f6beeadfe | 固定 v1.6.1 Linux release SHA256，CI 獨立測試 get_state。 |

Run 45980031044 的 Ubuntu 固定檔案驗收、OpenCode ACP 與 Pi RPC 通過。Hermes 原始 pip 安裝遇到上游 Pillow 固定版本衝突，後續依固定原始碼的 uv.lock 重跑。推理槽與根任務 token 共池由 SQLite 跨重啟保留，未確認推理停止時禁止重用。MLX/oMLX 的 loopback HTTP 行為經本機協定測試；macOS 公開 Runner 另執行小型隨機權重 Llama 原生 MLX 運算。

Run 45980639212 的 hosted macOS 原生 MLX CPU 測試通過。Hermes 固定版本要求 editable 安裝，後續測試沿用上游 lock 並唯讀掛載固定來源。新增 complete-evidence、replay-evidence、版本化模型設定升級與回滾。DirectSDK adapter 僅在已配置受信任報價、PUBLIC 任務及零成本保證時可進入傳輸；本輪不使用個人憑證或雲端推理。

verify-file 為可安裝 CLI 原生驗收入口，只接受受信任 AcceptanceSpec 指定的固定 Git blob。它核對工作區、租約及 Git 物件，以控制器複本執行唯讀測試，成功後產生簽章 Manifest。正式一般指令執行仍由 GovernedSandboxRunner 拒絕，直到其獨立安全驗收通過。時序證據關係支援矛盾證據、逐筆重播與時間序號查詢。訊息收件端可確認、重啟去重。

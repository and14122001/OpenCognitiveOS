# v8.6 工程架構與開發計畫（提案）
狀態：設計，未實作。承接 v8.4，不變更既有治理。
## 架構
採單機 modular monolith 起步，程序隔離的 adapters，不先引入微服務。src/opencognitiveos/{kernel,contracts,providers,agents,execution,world,knowledge,verification,evolution,interfaces,storage}。Python 3.15 為候選，實際支援矩陣依依賴測試。初期標準庫 SQLite/unittest/asyncio。
核心唯一寫入任務狀態；Hermes 只是規劃/推理適配器，不啟用其獨立派工或不受控工具。ACP 為優先候選，JSON-RPC 次之，HTTP/SSE 用於必要介面。無法限制工具或網路者不得進正式環境。
## 端到端
提交→契約驗證→政策/資料分類→讀取世界快照→計畫候選→DAG/預算/父目標驗證→資源保留與租約→模型/worker 推理→工具意圖→政策與租約檢查→隔離執行→結果事件→獨立測試→證據封存→人工審查 Draft PR→結案。失敗分類後有限重試；不明外部結果先對帳。等待子任務或授權時釋放運算槽；取消向下傳播。
狀態：SUBMITTED, PLANNED, READY, RUNNING, WAITING_APPROVAL, WAITING_CHILDREN, VERIFYING, RETRY_WAIT, NEEDS_RECONCILIATION, SUCCEEDED, FAILED, CANCELLED。狀態轉移需 CAS revision + fencing epoch；終態不可任意重開。
## 契約
TaskSpec: schema_version, project_id, repo_id, task_id, parent_id, base_sha, goal, acceptance_hash, allowed_paths, data_class, allowed_providers, allowed_tools, deadline, budget, dependencies.
Event: event_id, type, schema_version, task_id, execution_id, epoch, sequence, causation_id, correlation_id, occurred_at_utc, payload_ref, payload_hash.
ToolIntent: intent_id, execution_id, epoch, tool, args_hash, approval_ref, idempotency_key. EffectReceipt: intent_id, outcome, external_ref, evidence_hash. 不承諾跨外部系統 exactly-once。
Provider: capabilities(), infer(request), cancel(run_id), quota() (unknown 合法)。Worker: start(spec), events(run_id), cancel(run_id), reconcile(run_id)。World: observe(), snapshot(), predict(snapshot, action, horizon), compare(observation,prediction)。預測與事實分離；不確定性未知不得捏造。
SQLite: projects/tasks/dependencies/executions/leases/approvals/events/outbox/effects/artifacts/provider_runs/world_observations/predictions/knowledge。交易內提交狀態與 outbox；consumer 去重，外部操作以 receipt 對帳。原始證據存內容雜湊檔案；UTC 時間與版本分離。SQLite 不放雙向雲同步資料夾。
## 安全與預算
雲端須逐任務批准，綁資料範圍、provider、期限；額外 API 成本 0；額度未知不得自動上雲。agy/DirectSDK 共用訂閱配額視為同一資源池，尚待實測。禁用非官方憑證轉接及自動購買。環境 allowlist、工作區隔離、網路預設拒絕、子程序群組取消、獨立測試規格保護；worktree 不等於 sandbox。
初期候選限制：2 個 worker、1 個地端推理、深度 3、每節點分支 3、整棵樹 40 節點含根、最多 3 次修復、租約 120 秒、心跳 30 秒。皆為待實測設定；重試不能擴大預算。
## 世界模型與演進
先做軟體世界：Git SHA、依賴、建置及測試快照；其他領域按需求接入。時序、跨專案、層級、抽象、因果以 typed edges 表示，不需要實體五維資料庫。因果假設需干預或可識別條件。事件回饋用於候選知識，不直接修改正式規則。新模型/工具/流程在隔離 lab 與固定 baseline 等預算比較；授權後升級、可回滾。
## 里程碑與驗收
M0 契約、ADR、授權與安全威脅模型。M1 mock provider + SQLite 狀態機、CLI、單任務離線閉環；重複提交、重啟、過期租約、取消、故障對帳測試。M2 Git worktree/sandbox、獨立驗收與證據，測試竄改及越權拒絕。M3 Hermes ACP、oMLX、Pi/OpenCode，能力探測和上下文交接。M4 Google AI Pro agy/DirectSDK、Codex 候選，S49–60 配額、憑證、多模態及七天耐久。M5 DAG 遞迴、軟體世界模型、時序圖譜。M6 演進 lab、模型與工作流程 A/B、跨設備及備份復原。
每階段須提交可重現命令、原始日誌、SHA、通過/失敗及限制；未經驗收不進下一個生產階段。S01–S60 詳細測試契約仍待建立。無自動 merge 或未批准付費 CI。
## 參考（文件查閱，未重現上游）
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md
- https://github.com/chankov/agent-fleet/blob/main/docs/ARCHITECTURE.md
- https://github.com/chryzxc/hermes-worker-runtime
- https://github.com/dwjorgeb/hermes-orchestrator
- https://github.com/CCDevelopForFun/agent-controller
先驗證上游 commit、license、實際功能與安全；不引入第二個生命週期權威。

## v8.6 動態認知網路增補
以 [ADR-0002](adr/ADR-0002-governed-cognitive-network.md) 為新版認知層規格，保留 ADR-0001 單一執行權威。M1.6 Run #6 15/15 CI success；未合併。M2 安全隔離先行，M3 訊息代理、M5 分叉與動態拓撲、M6 等預算消融；新增 S61–72 Pending。不得將設計當成完成實作。既有 S01–60 不改號；M4 S43–48 依 M1.6 ACCEPTANCE_MATRIX，S49–60 依該矩陣分配，修正舊工程計畫的編號歧義。

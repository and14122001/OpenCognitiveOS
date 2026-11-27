# OpenCognitiveOS 威脅模型（M0–M1.6）

* **版本**：0.1.6.dev1
* **範圍**：單機離線 Kernel、SQLite 持久化層、PolicyGate、IndependentVerifier、Mock Provider、CLI

## 1. 威脅清單與緩解措施

| 編號 | 威脅情境 | 風險等級 | 緩解機制（M0–M1.6 實作） | 對應測試 |
| :--- | :--- | :--- | :--- | :--- |
| T01 | 未授權雲端 Provider 或僅憑 `cloud_approved=True` 繞過預算/配額上雲 | 高 | M1 預設完全停用所有 `is_cloud=True` Provider；未來授權亦須綁定 deadline、非 `RESTRICTED` 資料、已驗證計費模式與非未知配額 | `test_cloud_provider_disabled_in_m1_even_when_spec_approved` |
| T02 | 未授權工具、路徑穿越（`..`/絕對路徑）或參數與宣告路徑不一致 | 高 | `PolicyGate.check_tool` 直接解析 `args` 內路徑並比對 `target_path`，拒絕絕對路徑、`..`、反斜線與越權路徑 | `test_path_traversal_and_absolute_paths_rejected`, `test_tool_args_and_target_path_mismatch_rejected` |
| T03 | 省略 `epoch` 或租約過期後 Worker 仍寫入狀態／完成任務（Split-Brain） | 高 | `WORKER_FENCED_TRANSITIONS` 強制驗證 `worker_id`、`execution_id`、`epoch` 與未過期租約，Controller 亦不得越權執行 | `test_worker_omitting_epoch_or_execution_id_rejected`, `test_expired_lease_cannot_complete_task` |
| T04 | 兩名 Worker 同時搶占租約或併發重試超額 | 高 | `acquire_lease` 與 `schedule_retry` 皆於單一 `BEGIN IMMEDIATE` 交易內原子檢查與更新 | `test_two_workers_compete_for_lease`, `test_concurrent_retry_race_respects_budget_atomically` |
| T05 | 假造、跨任務、錯誤 execution 或衝突的 `EffectReceipt` | 高 | `record_effect_receipt` 驗證 Intent 存在性、歸屬、`execution_id`、`epoch`、`PENDING` 前置態與 `rowcount == 1` | `test_nonexistent_cross_task_wrong_exec_and_conflicting_receipt_rejected` |
| T06 | 重送 `ToolIntent` 產生重複副作用，或 `PENDING`/`UNKNOWN` 效果於重啟後被盲目重試 | 高 | 確定性 `idempotency_key` 去重；`recover_on_restart` 偵測未結算效果一律轉入 `NEEDS_RECONCILIATION` | `test_deterministic_idempotency_key_deduplicates_same_op_and_allows_distinct_op`, `test_pending_intent_on_restart_enters_reconciliation_not_ready` |
| T07 | 跳過獨立驗收直接宣告任務成功 | 高 | 禁止 `RUNNING -> SUCCEEDED` 與 `NEEDS_RECONCILIATION -> SUCCEEDED`；須經 `VERIFYING` 與 `IndependentVerifier` | `test_running_cannot_transition_directly_to_succeeded_and_verifier_rejects_empty_effects` |
| T08 | SQLite 連線未關閉導致檔案句柄洩漏或雙向雲同步損毀 | 中 | 所有連線以 `open_connection()` / `closing()` 明確關閉；禁止將 DB 置於雲端同步資料夾 | `test_sqlite_connections_closed_zero_resource_warnings` |

## 2. M1 明確限制（留待 M2–M6）

* **符號連結（Symlink）與 OS Sandbox**：M1 僅提供詞法層級相對路徑檢查，未解析真實檔案系統 symlink 與 OS 層級網路/檔案隔離（留待 M2 `S21–S30`）。
* **外部 Handler Exactly-Once**：Outbox 提供 at-least-once 派送與消費端去重，不宣稱外部系統具備 exactly-once。

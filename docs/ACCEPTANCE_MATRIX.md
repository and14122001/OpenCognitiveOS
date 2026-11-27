# OpenCognitiveOS S01–S60 驗收矩陣（M0–M1.6）

> **原則**：驗收規格與測試結果分離。未實作或未實際執行通過之項目一律標示 `Pending`。
> **實測環境**：`Python 3.11.2`（`PYTHONWARNINGS=error::ResourceWarning`，離線標準庫）。Python 3.12–3.15 相容性待驗證。

## 1. M0–M1 範疇（S01–S20）

| 編號 | 里程碑 | 驗收情境規格 | 實際驗證測試 | 狀態 |
| :--- | :--- | :--- | :--- | :--- |
| S01 | M0 | 版本化契約定義（含 `VerificationReceipt`, `ToolIntentSubmission`） | `contracts/models.py` + `unittest` | Passed (Python 3.11.2) |
| S02 | M0 | ADR-0001 與安全威脅模型文件完成並與實作一致 | `ADR-0001`、`THREAT_MODEL.md`、`pyproject.toml` | Passed (Python 3.11.2) |
| S03 | M1 | SQLite WAL、外鍵、`integrity_check` 與連線明確關閉（無 `ResourceWarning`） | `test_migrations_idempotent_and_pragmas`, `test_sqlite_connections_closed_zero_resource_warnings` | Passed (Python 3.11.2) |
| S04 | M1 | Schema Migration（v1–v4）追蹤與重複執行冪等性 | `test_migrations_idempotent_and_pragmas` | Passed (Python 3.11.2) |
| S05 | M1 | 任務唯一鍵 `repo_id::task_id`：同內容冪等、異內容拒絕 | `test_unique_key_idempotent_and_conflict_rejection` | Passed (Python 3.11.2) |
| S06 | M1 | 多連線同時提交相同任務的並發安全 | `test_concurrent_multi_connection_task_submission` | Passed (Python 3.11.2) |
| S07 | M1 | 狀態機合法轉移、終態不可重開、禁止繞過 `IndependentVerifier` 直達 `SUCCEEDED` | `test_illegal_transition_and_terminal_immutability`, `test_worker_cannot_bypass_verifier_to_succeed` | Passed (Python 3.11.2) |
| S08 | M1 | CAS + Fencing（`worker_id`, `execution_id`, `epoch`, `revision`）強制驗證 | `test_worker_omitting_epoch_or_execution_id_rejected`, `test_expired_lease_cannot_complete_task` | Passed (Python 3.11.2) |
| S09 | M1 | 雙 Worker 競爭租約僅一者成功 | `test_two_workers_compete_for_lease` | Passed (Python 3.11.2) |
| S10 | M1 | 租約過期後舊 Worker 被拒 | `test_stale_worker_rejected_after_lease_expiration_and_epoch_advance` | Passed (Python 3.11.2) |
| S11 | M1 | 交易中斷同步回滾（含 Receipt 與 VerificationReceipt 原子提交） | `test_transaction_interruption_rolls_back_state_events_and_outbox`, `test_verify_and_complete_atomic_rollback_on_crash` | Passed (Python 3.11.2) |
| S12 | M1 | 重啟補派送待處理 Outbox | `test_restart_dispatches_pending_outbox_and_deduplicates_redelivery` | Passed (Python 3.11.2) |
| S13 | M1 | Outbox 多消費者去重與 Handler 失敗重試 | `test_multi_consumer_outbox_and_handler_failure_retry` | Passed (Python 3.11.2) |
| S14 | M1 | 確定性冪等鍵區分 `CREATED`/`PENDING_RECONCILIATION`/`SETTLED`，Receipt 完整性與對帳後經 `VERIFYING` | `test_tool_intent_distinguishes_created_pending_and_settled`, `test_nonexistent_cross_task_wrong_exec_and_conflicting_receipt_rejected` | Passed (Python 3.11.2) |
| S15 | M1 | 重啟恢復（`RUNNING`, `VERIFYING`, `RETRY_WAIT`, `PENDING` Intent） | `test_restart_recovery_from_persisted_sqlite_only`, `test_pending_intent_on_restart_enters_reconciliation_not_ready`, `test_verifying_state_restart_recovery` | Passed (Python 3.11.2) |
| S16 | M1 | 取消與完成競態由 CAS 決定唯一勝者 | `test_cancel_and_complete_race_resolved_by_cas` | Passed (Python 3.11.2) |
| S17 | M1 | 原子有限重試與併發重試競爭保護 | `test_retry_preserves_budget_and_permissions`, `test_concurrent_retry_race_respects_budget_atomically` | Passed (Python 3.11.2) |
| S18 | M1 | M1 無條件停用雲端、路徑元件邊界（拒絕 `.src/...`、保留 `.hidden`）與參數一致檢查 | `test_cloud_disabled_unconditionally_no_runtime_bypass`, `test_path_normalization_rejects_dot_prefix_spoof_and_allows_hidden_dir` | Passed (Python 3.11.2) |
| S19 | M1 | SQLite 備份與還原完整性 | `test_backup_and_restore` | Passed (Python 3.11.2) |
| S20 | M1 | 明確 Mock 驗收條件、可獨立重播證據、Runner 例外處理與 CLI 離線閉環 | `test_verifier_explicit_criteria_and_independent_replay`, `test_runner_handles_provider_exception_and_verification_failure`, `test_cli_offline_end_to_end` | Passed (Python 3.11.2) |

## 2. M2–M6 範疇（S21–S60）

| 編號 | 里程碑 | 驗收情境規格 | 狀態 |
| :--- | :--- | :--- | :--- |
| S21–S30 | M2 | Git worktree、OS Sandbox、Symlink 真實解析、網路預設拒絕、獨立測試雜湊保護與 SHA 證據封存 | Pending |
| S31–S42 | M3 | Hermes ACP、地端 MLX/oMLX、Pi/OpenCode、併發槽位治理與基線比較 | Pending |
| S43–S48 | M4 | Google AI Pro agy/DirectSDK、Codex OAuth、共用配額池與資料分類外傳阻斷 | Pending |
| S49–S54 | M4–M5 | 軟體世界模型、DAG 遞迴控制（深度 3/分支 3/總節點 40）、時序因果知識圖譜 | Pending |
| S55–S60 | M4–M6 | Evolution Lab A/B 比較、授權升級與回滾、多模態契約、跨設備復原與七天耐久測試 | Pending |

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

## 3. PR #5 工程實測（2026-11-27）

本節沿用 PR #4 以前 M2 任務表的逐項定義。Drive R4 交付物以 S26/S28/S29/S30 表示規格、CAS、Manifest、反例回歸，故同時記錄功能名稱。原 M1 歷史結果保留於上方。安全結論限於明列的平台、入口和案例。

| 編號 | 已執行 | 尚待驗收 |
|---|---|---|
| S21 | 固定完整 commit SHA、detached worktree、雙向 Git metadata | 目標機部署 |
| S22 | SQLite 工作區唯一性、租約、清理競態、不明工作區留存 | 已派送工作區的自動安全回收 |
| S23 | traversal、symlink、硬連結、競態防護回歸；Linux 只將控制器取得的固定 Git blob 提供給 worker | Windows junction 的原生工具路徑 |
| S24 | Ubuntu 正式 broker 真實執行 bubblewrap 和 cgroup v2，未知後端拒絕 | Darwin 與 Windows 的原生隔離 |
| S25 | Linux 獨立 network namespace、宿主網路預設拒絕 | 跨平台原生等價驗證 |
| S26 | Linux 清除繼承環境、白名單限制；R4 AcceptanceSpec 驗簽及竄改回歸 | 目標部署的金鑰佈建與輪替 |
| S27 | 批准的固定單檔 Python 綁定 task、lease、execution、epoch、worktree、spec 與 broker effect receipt；兩個原生工作槽 | 一般編輯／建置工具代理與完整工作區輸出 |
| S28 | Linux 真實子程序逾時及終止、非零結果留存；R4 CAS 跨程序容量、去重及重驗 | 未知 unit 狀態持續保留供對帳 |
| S29 | 唯讀獨立驗證固定 Git blob、環境與 Manifest | 可執行任意專案測試套件的獨立驗收器 |
| S30 | 兩把獨立信任金鑰、簽章與 SHA 重驗、CAS 損毀拒絕、狀態／收據／Outbox 交易回滾、重播 | 完整工具鏈與目標設備驗收 |
| S31–42 | Hermes 與 OpenCode 實際 ACP initialize、session/new；Pi RPC get_state；Apple arm64 MLX CPU 與 MLX-LM HTTP 完成和用量結算 | 完整代理推理與取消、oMLX 本體、目標設備模型比較 |
| S43–48 | 零成本任務授權、資料分類、共池、保留與結算；DirectSDK 模擬傳輸 | 真實 agy、DirectSDK、Codex OAuth 與帳戶零成本權益 |
| S49–54 | DAG 前置依賴、深度 3／分支 3、父額度共享；可重播證據圖與矛盾邊 | 完整世界模型與端對端多代理協作 |
| S55–60 | Linux 原生工作證據在另一台 macOS 主機還原並重播、版本化發布與回滾、配對小模型實測；文字／PCM／RGB 附件契約及快照 | 真實影音模型輸入、目標設備災難復原與七天耐久 |
| S61–64 | SQLite 任務訊息、作用域與 epoch、TTL、順序與確認、容量限制、父 token／步數保留 | 完整拒絕稽核及跨群組壓力測試 |
| S65–69 | 隔離推理快照、拓撲版本、取消級聯、證據候選 CLI、模型／策略固定與切換防護 | 內容交接、候選變更獨立驗證及完整共同錯誤測試 |
| S70–72 | 兩輪事前登記，每輪 30 題 × 2 seeds × 2 策略，token 與時間各自比較；本次不升級 | 六策略消融、盲測、KV/通信成本完整量測、七天耐久 |

本地重跑、逐項日誌與上游版本見 [驗證紀錄](validation/m2/STATUS.md)。模型原始輸出見 [配對實驗](validation/ab/README.md)。

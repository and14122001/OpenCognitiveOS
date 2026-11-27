# ADR-0001：單一確定性執行權威與 SQLite 持久化架構（M0–M1.6）

* **狀態**：Accepted（M0–M1.6 基線）
* **日期**：2026-11-27
* **參考規格**：`AGENTS.md`、`GEMINI.md`、`docs/ENGINEERING_PLAN.zh-TW.md`、`docs/tasks/M0-M1.md`

## 決策摘要

1. **強制獨立驗收與持久化 `VerificationReceipt`**：禁止 Worker 透過 `RUNNING -> VERIFYING -> SUCCEEDED` 繞過 `IndependentVerifier`。`verify_and_complete` 在單一交易內驗證持久化 `effects` 與明確 Mock 驗收條件（可透過 `replay_verify` 獨立重算重播），將 `verification_receipts`（綁定 `task_unique_key`、`acceptance_hash`、`execution_id`、`epoch`、`revision` 與 `evidence_hash`）與 `SUCCEEDED` 狀態原子提交。
2. **路徑元件邊界正規化**：禁止使用 `str.lstrip("./")`；改以僅剝除精確前綴 `./` 後比對 `PurePosixPath.parts` 元件邊界，拒絕 `.src/opencognitiveos/private` 與字首混淆目錄，同時保留合法隱藏目錄（如 `src/opencognitiveos/.hidden/...`）。Symlink 真實解析與 OS Sandbox 留待 M2。
3. **M1 無條件停用雲端 Provider**：`PolicyGate.check_provider` 於 M1 預設無條件拒絕所有 `is_cloud=True` Provider，不提供 `allow_cloud_in_runtime` 繞過旗標。
4. **確定性 `ToolIntent` 狀態區分**：`record_tool_intent` 回傳 `ToolIntentSubmission`，明確區分 `CREATED`、`PENDING_RECONCILIATION` 與 `SETTLED`；已結算操作絕不再度執行外部效果。
5. **SQLite 持久化、Fencing 與 Outbox**：維持 `WAL`、`foreign_keys=ON`、`busy_timeout=5000`、Migration v1–v4、`open_connection()`/`closing()` 明確關閉（零 `ResourceWarning`）、原子重試與多消費者 Outbox（不宣稱外部 exactly-once）。

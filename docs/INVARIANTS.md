# OpenCognitiveOS M0–M1.6 核心不變量與信任邊界（INVARIANTS）

* **版本**：`0.1.6.dev1`（PEP 440 合規；契約版本 `1.6.0`）
* **範圍**：單機離線確定性執行核心（M0–M1.6）。**不含** M2 OS Sandbox 與 Symlink 真實解析。

## 1. 公開操作 × 合法狀態授權矩陣（`OPERATION_ALLOWED_STATES`）

| 公開操作 | 允許狀態 | 禁止狀態（一律拒絕） |
| :--- | :--- | :--- |
| `acquire_lease` | `READY`, `RUNNING`, `VERIFYING` | `SUBMITTED`, `PLANNED`, `WAITING_APPROVAL`, `WAITING_CHILDREN`, `RETRY_WAIT`, `NEEDS_RECONCILIATION`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `heartbeat_lease` | `RUNNING`, `VERIFYING` | 其餘所有狀態 |
| `record_tool_intent` | `RUNNING`（僅限已授權 `RUNNING` execution） | 其餘所有狀態（含 `SUBMITTED`, `READY`, `VERIFYING` 等） |
| `record_effect_receipt` | `RUNNING` | 其餘所有狀態 |
| `verify_and_complete` | `VERIFYING` | 其餘所有狀態 |
| `reconcile_effect` | `NEEDS_RECONCILIATION` | 其餘所有狀態 |
| `schedule_retry` | `RUNNING`, `VERIFYING`, `RETRY_WAIT` | 其餘所有狀態 |
| `cancel_task` | 所有非終態（`SUBMITTED`..`NEEDS_RECONCILIATION`） | `SUCCEEDED`, `FAILED`, `CANCELLED` |

## 2. 跨表一致性審查 vs 真正防竄改機制 vs M2 隔離邊界

1. **單一交易跨表一致性審查（`audit_database_consistency`）**
   * 目的：在單一 SQLite 一致性讀取交易（`BEGIN DEFERRED`）內，檢查 `tasks`、`verification_receipts` 與 `effects` 是否一致，並完整比對 `tasks.result_json`（包含 `output_hash`、`verification_id`、`verification_evidence_hash` 之欄位存在性、無多餘欄位、值與型別）。
   * **重要安全聲明**：本檢查屬於**資料庫跨表一致性審查**（偵測部分資料表遭誤寫、毀損或單表竄改），**不宣稱**可防禦已取得完整 SQLite 檔案寫入權限的攻擊者（此類攻擊者可同時改寫所有資料表）。真正的外部防竄改須依賴外部 Append-Only / WORM 儲存或硬體簽章。
2. **歷史快照證據重播（`replay_verification`）**
   * 僅依據 `verification_receipts` 內封存的歷史快照與 `TaskSpec` 重算驗收條件與全欄位雜湊；不等同於實時資料庫一致性審查。
3. **尚未實作的 OS 安全隔離（Pending M2）**
   * M1.6 僅於 `PolicyGate` 執行詞法相對路徑元件邊界檢查。真實 Symlink 解析、Git worktree、網路預設拒絕與 OS Sandbox 留待 M2（`S21–S30`）。

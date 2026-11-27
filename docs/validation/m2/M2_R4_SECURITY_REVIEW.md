# OpenCognitiveOS M2 R4 獨立安全審查報告（M2_R4_SECURITY_REVIEW）

* **審查時間（UTC ISO8601）**：`2026-11-27T09:19:00Z`
* **受審基線**：`OpenCognitiveOS-M0-M1.zip` + `M2_consolidated_candidate.patch`（Base Commit `f943fd1dc817c40b4cf0afdc92a8b302862013aa`）
* **審查範圍**：S26（`acceptance.py`）、S28（`evidence_store.py`）、S29（`manifest.py`）、S30（`tests/test_m2_s26_s28_s29_s30.py`）；未修改 S22–S25、S27 及 F16–F19。

---

## 1. 安全設計與實作審查結果

1. **S26 獨立驗收規格防竄改（`src/opencognitiveos/verification/acceptance.py`）**
   * `AcceptanceSpec` 採明確版本（`schema_version = "2.0.0"`）與確定性 JSON 序列化，以 SHA-256 完整雜湊保護 `project_id`、`repo_id`、`task_unique_key`、`base_sha`、`policy_hash`、`verifier_id`、`criteria`、`created_at_utc`、`expires_at_utc`（不再僅雜湊 `goal`）。
   * 任務進入 `RUNNING` 等執行態後禁止修改規格；Worker 角色呼叫 `register_spec` 或 `assert_verifier_authority` 直接被 `PolicyDeniedError` 拒絕；規格缺失、過期或雜湊不符一律 Fail Closed。
2. **S28 內容定址證據儲存（`src/opencognitiveos/verification/evidence_store.py`）**
   * 儲存結構為 `evidence/sha256/<prefix>/<digest>`，透過 `O_CREAT | O_EXCL` 暫存檔 + `os.fsync()` + `os.replace()` 原子寫入，讀取與去重前均重新計算 SHA-256。
   * 強制阻擋路徑穿越、`st_nlink > 1` 硬連結、符號連結、特殊檔案（FIFO/socket/device）及機密憑證模式（`OPENAI_API_KEY`、`GITHUB_TOKEN`、`AIza...`、`ghp_...`、`sk-...`、`AKIA...`）；Worker 禁止直接寫入或覆寫證據。
   * **原子性邊界誠實聲明**：檔案系統 CAS 與 SQLite 資料庫不具跨系統分散式 2PC 原子交易；實作先寫入 CAS 再於 SQLite `BEGIN IMMEDIATE` 提交 `evidence_manifests`，儲存失敗絕不產生成功驗收紀錄，重啟時可透過 `recover_orphaned_staging()` 清理未完成 `.tmp-*` 暫存檔。
3. **S29 Git SHA 與執行環境綁定（`src/opencognitiveos/verification/manifest.py`）**
   * `capture_trusted_git_state` 由控制器／Verifier 直接執行受控 `git`（停用 hooks 與外部設定注入）取得 `tested_commit_sha`、`tested_tree_sha` 與 `git status --porcelain -z`；若包含未提交變更則產生內容快照並存入 S28 CAS，拒絕將髒工作區冒充為乾淨 `HEAD`。
   * `EvidenceManifestRegistry` 於 SQLite 交易內原子驗證活躍 Lease 與 `fencing_epoch`，拒絕跨任務、跨執行、過期租約與錯誤 Epoch 重播。
   * **密碼學邊界誠實聲明**：SHA-256 雜湊完整性不等於來源真實性（Origin Authenticity），亦無法防禦同時具備完整控制器與 SQLite 資料庫寫入權限的本地攻擊者。

## 2. S30 安全回歸測試覆蓋矩陣

`tests/test_m2_s26_s28_s29_s30.py`（11 項測試方法）已完整覆蓋 S30 所列 19 項必要安全情境，且所有測試皆驗證實際防護拒絕行為（非僅函式存在），未將歷史漏洞重現計入通過數。

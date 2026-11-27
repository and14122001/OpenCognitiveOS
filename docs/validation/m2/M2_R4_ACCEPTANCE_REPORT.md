# OpenCognitiveOS M2 R4 驗收報告（M2_R4_ACCEPTANCE_REPORT）

* **報告時間（UTC ISO8601）**：`2026-11-27T09:19:00Z`
* **執行環境**：`Linux 6.12.105+ x86_64`、`Python 3.11.2`、`git version 2.39.5`
* **測試命令**：`PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`
* **實測結果**：共 `112` 項測試 — **107 PASS、0 FAIL、5 SKIP**（`Exit Code: 0`，零 `ResourceWarning`）

---

## 1. 狀態分類總表

### 已實作且驗收通過
* **S26 獨立驗收規格防竄改**（`src/opencognitiveos/verification/acceptance.py`）：版本化確定性序列化、完整 SHA-256 雜湊、任務開始後不可變、Worker 偽造 PASS 與跨任務／跨專案／過期規格拒絕，由 S30 測試實證通過。
* **S28 內容定址證據儲存**（`src/opencognitiveos/verification/evidence_store.py`）：`evidence/sha256/<prefix>/<digest>` 結構、去重、原子寫入、讀取重驗、symlink/hardlink/traversal 拒絕、容量與串流限制、並行寫入與 staging 恢復、憑證攔截，由 S30 測試實證通過。
* **S29 Git SHA 與執行環境綁定**（`src/opencognitiveos/verification/manifest.py`）：完整 18 欄位 `EvidenceManifest`、可信 Git Commit/Tree SHA 擷取、未提交變更快照、執行環境指紋、跨執行／過期租約／錯誤 Epoch 拒絕，由 S30 測試實證通過。
* **S30 獨立安全與反例回歸測試**（`tests/test_m2_s26_s28_s29_s30.py`）：11 項測試覆蓋全部 19 項必要情境，全數通過。
* **既有回歸基線保留**：原 101 項基線測試中 96 項在本機 Linux 環境通過（0 FAIL）。

### 已實作但未驗收（環境受限／SKIP）
* **S22–S25 / S27 / F16–F19 中依賴 `bwrap` 或 macOS / Windows 實機之 5 項測試**：本機容器未安裝 `bwrap` 且非 macOS/Windows，故依原有測試邏輯標記 `SKIP`（共 5 項），**未將 SKIP 視為實機驗收通過**；上述模組程式碼本次完全未修改。

### 尚未實作
* **S31–S60（M3–M6 範疇）**：依本次範圍未開發。

### 安全缺陷與阻塞
* **跨系統原子性與資料庫寫入攻擊者邊界**：檔案系統 CAS 與 SQLite 之間不具跨系統分散式原子交易；SHA-256 完整性不可防禦同時擁有完整控制器與 SQLite 寫入權限之攻擊者（已於 `M2_R4_SECURITY_REVIEW.md` 明確揭露）。
* **多版本 Python 實測限制**：本機僅安裝 `Python 3.11.2`，Python 3.12–3.15 須待 GitHub Actions 矩陣實測。

### GitHub 實際同步狀態
* **未直接推送至 GitHub**：因本沙盒無對外網路與 GitHub 寫入憑證，未修改遠端 `main` 或 `feat/m2-security`，已於本機建立獨立分支 `feat/gemini-m2-r4` 並產出完整 `M2_R4_S26_S28_S29_S30.patch` 供整合。

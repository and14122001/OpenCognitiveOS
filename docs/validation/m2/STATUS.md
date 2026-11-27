# PR #5 工程驗證紀錄

| 檢查點 | 結果與證據 |
|---|---|
| 既有 PR #4 的 7ab15cb | 63/63。[日誌](pr4.log) |
| 重建 R3/R4 與修正 | 118 項，113 通過、5 跳過。從 d929527 依序 git apply --check 並套用 consolidated（含 R3）、R4、CAS、F25、F20 partial、F21 registration。[日誌](recovery.log)／[來源摘要](sources.sha256) |
| Manifest | ZIP、consolidated、recovery log、R4 patch、原始測試日誌雜湊相符。兩份 Google 文件目前匯出的位元組與原 Markdown 摘要不同，原檔待補。 |
| 可攜回歸 | 129 項，124 通過、5 跳過。這五項針對已隔離的舊 unshare 後端。正式 broker 另設原生測試。[逐項日誌](final-regression.log) |
| Ubuntu 正式入口 | bubblewrap、cgroup v2、錯誤輸出證據、子程序逾時、獨立驗收、原子 SUCCEEDED 及重播。[Run 45984405670 / Linux job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456206728) |
| 三個上游程序 | Hermes 8fc03744 與 OpenCode v1.19.6 的 ACP session/new、Pi v1.6.1 的 RPC get_state。固定來源，在無網路及無憑證 namespace 內執行。[Hermes](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456207003)／[OpenCode](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456206991)／[Pi](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456207029) |
| MLX-LM | macOS arm64 原生 MLX CPU、合成小模型、真實 HTTP server 與用量結算。[Run 45983216217 / MLX job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45983216217/job/140452225425) |
| 實驗 | 公開 SmolLM2 CPU 浮現輸出格式地板效應。實驗室結果禁止升級。[預註冊與逐筆結果](../ab/README.md) |

回歸命令為 `PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`。CI 的 Linux 原生入口為 `bash tools/verify_linux_isolation.sh` 與 `PYTHONPATH=src python3 tools/test_native_worker.py -v`。舊 Run 45983216217 的 Linux timeout 案例發現 systemd 停止時可能回傳零。Run 45984405670 已驗證修正，逾時一律回傳 124。保留原失敗紀錄。

完整邊界見[專案驗收矩陣](../../ACCEPTANCE_MATRIX.md)。控制器與 SQLite 寫入權限屬於信任邊界，worker 在獨立 OS 程序執行。CAS 先寫入不可變物件、SQLite 在單一交易提交可見證據引用、任務、收據與 outbox。斷電可留下未引用 CAS 物件。

# PR #5 工程驗證紀錄

| 檢查點 | 結果與證據 |
|---|---|
| 既有 PR #4 的 7ab15cb | 63/63。[日誌](pr4.log) |
| 重建 R3/R4 與修正 | 118 項，113 通過、5 跳過。從 d929527 依序 git apply --check 並套用 consolidated（含 R3）、R4、CAS、F25、F20 partial、F21 registration。[日誌](recovery.log)／[來源摘要](sources.sha256) |
| Manifest | R4 Manifest 指定的 3 個基線摘要與 4 個交付物摘要全部相符。兩份 Google 文件以 text/markdown 匯出，還原 Google 加入的反斜線、行尾空白及末端 LF；驗收報告另移除三級標題與項目間的空行。重建位元組與封存 SHA256 完全相符。[驗收報告](M2_R4_ACCEPTANCE_REPORT.md)／[安全審查](M2_R4_SECURITY_REVIEW.md)。 |
| 可攜回歸 | 130 項，125 通過、5 跳過。這五項針對已隔離的舊 unshare 後端。正式 broker 另設原生測試。[逐項日誌](final-regression.log) |
| Ubuntu 正式入口 | bubblewrap、cgroup v2、錯誤輸出證據、子程序逾時、獨立驗收、原子 SUCCEEDED 及重播。[Run 45984405670 / Linux job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456206728) |
| 三個上游程序 | Hermes 8fc03744 與 OpenCode v1.19.6 的 ACP session/new、Pi v1.6.1 的 RPC get_state。固定來源，在無網路及無憑證 namespace 內執行。[Hermes](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456207003)／[OpenCode](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456206991)／[Pi](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45984405670/job/140456207029) |
| 上游代理文字回合 | OpenCode、Pi 在無外網 namespace 透過固定 CPU GGUF 取得回覆；Hermes 以真正 64K 以上 context 的 Qwen3.5-0.8B 完成文字回合，測量 9 個本地生成 tokens。Run 45988070964 的 Hermes job 後續於釋放模型程序超時，已修正清理流程，整個舊 job 計為失敗。後續完整 job 成功：[Hermes](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45988665073/job/140470625723)、[Pi](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45988665073/job/140470625678)、[OpenCode](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45988665073/job/140470625730)。 |
| Codex | 官方 0.172.0 二進位檔 SHA256 固定，無網路及真實憑證下完成 initialize 與 account/read，確認未登入、禁止派送。[Codex job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45988665073/job/140470625926) |
| Google DirectSDK | 官方 google-genai 3.6.1，真實 SDK 送往本機合成服務。未知費用零次傳輸、核准後一次傳輸並結算 4 tokens。[本機日誌](google-sdk-loopback.log) |
| MLX-LM | macOS arm64 原生 MLX CPU、合成小模型、真實 HTTP server 與用量結算。[Run 45983216217 / MLX job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45983216217/job/140452225425) |
| oMLX | 公開 macOS Runner 實際回報 Metal available=True，固定 bc55984a 的上游 oMLX 完成合成小模型推理，結算 5 tokens。[Run 45985437591 / oMLX](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45985437591/job/140459728648) |
| 跨主機還原 | 原生 Linux fixture 的 SQLite 與 CAS 傳至另一台 macOS 標準 Runner，從 CI job output 取得獨立 SHA，還原後重播原始成功收據。[Run 45985437591 / restore job](https://github.com/and14122001/OpenCognitiveOS/actions/runs/45985437591/job/140459808508) |
| 實驗 | 公開 SmolLM2 CPU 浮現輸出格式地板效應。實驗室結果禁止升級。[預註冊與逐筆結果](../ab/README.md) |

回歸命令為 `PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`。CI 的 Linux 原生入口為 `bash tools/verify_linux_isolation.sh` 與 `PYTHONPATH=src python3 tools/test_native_worker.py -v`。舊 Run 45983216217 的 Linux timeout 案例發現 systemd 停止時可能回傳零。Run 45984405670 已驗證修正，逾時一律回傳 124。保留原失敗紀錄。

完整邊界見[專案驗收矩陣](../../ACCEPTANCE_MATRIX.md)。控制器與 SQLite 寫入權限屬於信任邊界，worker 在獨立 OS 程序執行。CAS 先寫入不可變物件、SQLite 在單一交易提交可見證據引用、任務、收據與 outbox。斷電可留下未引用 CAS 物件。

# CPU 參考模型配對實驗

原始 preregistration commit 為 a818cad100f310b488da31a8b259af4e5b76a468。模型為 bartowski/SmolLM2-135M-Instruct-GGUF 的 Q4_K_M，固定修訂 09816acd5d99df7be770d85ea30822623dab342c。GGUF SHA256 與 llama.cpp CPU release SHA256 見計畫。執行為單一 slot、四條 CPU 執行緒與 512 tokens context，兩種策略依任務及種子交錯執行。

初輪以純文字提示、換行停止。兩個資源條件的 120 筆觀察均未給出可評分的正確答案。此結果有地板效應。保留全部原文、用量、耗時及 RSS。安全性未獨立評估，升級資格關閉。第二輪另行預註冊模型官方對話模板與新題目。兩輪皆為小模型算術任務，不能代表工程代理或使用者設備的實際效能。

第二輪 preregistration 為 3b07adaf5c872dd7f351d6980c3b929ce890b40d。兩個試驗在預註冊的純整數評分規則下，各得 30 平、0 勝、0 負，單側符號檢定 p=1。部分回覆以句子包含正確數字，未滿足格式規格；此結果無法證明兩策略的算術能力相同。token 試驗單次採樣共 3,187 tokens、7.783 秒，三次獨立採樣共 9,544 tokens、23.415 秒；兩組上限相同，實耗不同。四次比較各有 120 筆觀察，共 480 筆。安全性未獨立驗收，保留原策略。

第三輪預註冊 4078955de63ce283091184b2004a7c7be720862d，執行碼與計畫先於推理提交。30 道新算術題、兩個 seeds、六種邏輯策略、兩種預算共 720 筆，所有最終整數均由相同 grammar 約束。每個策略在每種預算均為 0/60；未觀察到可升級收益。單一模型共用一個推理槽。每個 token 試驗各策略在相同 384-token 上限下，單 Agent 實耗 1,723 tokens，平面四次推理實耗 8,407 tokens（皆為 60 筆合計）。持續記錄每次 prompt/eval 時間；KV 配置 11.25 MiB，RSS 為採樣值，無逐核心能耗實測。此為 CPU 參考模型結果。完整盲測與獨立安全驗證仍待完成。

六策略逐筆資料在 six-strategies/，以 Base64 包裝 gzip 保留原始 JSONL。解碼後 SHA256、通過數、延遲、用量、訊息流量及呼叫數見 summary.json。範例命令為 `base64 -d tokens-single.jsonl.gz.b64 | gzip -dc | sha256sum`。

逐筆獨立重播命令為 `python3 tools/audit_strategy_results.py docs/validation/ab/six-strategies`。重新解碼 12 份封存、重建投票與整數評分、核對每筆資源上限；720 筆均一致。此為事後匿名評分重播。

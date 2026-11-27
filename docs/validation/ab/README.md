# CPU 參考模型配對實驗

原始 preregistration commit 為 a818cad100f310b488da31a8b259af4e5b76a468。模型為 bartowski/SmolLM2-135M-Instruct-GGUF 的 Q4_K_M，固定修訂 09816acd5d99df7be770d85ea30822623dab342c。GGUF SHA256 與 llama.cpp CPU release SHA256 見計畫。執行為單一 slot、四條 CPU 執行緒與 512 tokens context，兩種策略依任務及種子交錯執行。

初輪以純文字提示、換行停止。兩個資源條件的 120 筆觀察均未給出可評分的正確答案。此結果有地板效應。保留全部原文、用量、耗時及 RSS。安全性未獨立評估，升級資格關閉。第二輪另行預註冊模型官方對話模板與新題目。兩輪皆為小模型算術任務，不能代表工程代理或使用者設備的實際效能。

# 工程驗收矩陣

| 範圍 | 核對項目 | 本輪證據與限制 |
|---|---|---|
| PR #4 | 7ab15cb，63 項 | pr4.log |
| R3/R4 | 118 項，113 通過，5 跳過 | recovery.log。從 d929527 依序套用 consolidated、R4、CAS、F25、F20 partial、F21 registration，每層 git apply --check。PR #4 重疊檔按最終內容整合。歷史稽核用重現腳本保留於原始交付物。 |
| R4 Manifest | ZIP、consolidated、recovery log、R4 patch、原始測試日誌雜湊相符 | 兩份 Google 文件目前匯出的文字與原 Markdown 宣告的 SHA256 不同。原始 Markdown 位元組待取得。 |
| S21–S30 | 固定 Git SHA、工作區、epoch、CAS、Manifest capability 與舊入口拒絕已測 | OS 測試的五個 skip 仍保留；獨立 CI job 驗證 Linux 核心原語。Run 45978475746 的 15 組矩陣與 Linux 核心測試均成功。封存規格及 Manifest 採兩把控制面 HMAC 金鑰，回歸含錯誤金鑰、交易中斷、成功與封存後 CAS 遺失。正式 runner 整合 Pending。 |
| S31–S48 | 地端模型與雲端供應商 | ACP v1 初始化與 SQLite 零成本配額已做本地協定、雙任務共池與重放測試。真實 adapter、個別帳戶與費用證明待驗收。 |
| S49–S54、S61–S69 | 任務依賴、三層/每層三個子任務、父子步數預留 | 資料庫單交易執行；訊息、動態拓撲及推理資源治理待驗收。 |
| S55–S60、S70–S72 | SQLite 還原前驗證，無效來源保留目標 | 跨設備全量復原、配對基準、升級回滾及七天耐久待驗收。 |

本機回歸命令為 `PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`。

# ADR-0002 — v8.6 受治理動態認知網路
狀態：Proposed；2026-11-27 UTC 日期。承接 ADR-0001，禁止改動 M1 契約及既有測試。
基線：PR #2 head 28d86728435b005fe50a465fead607f1c5f00c43；PR-triggered Run #6 https://github.com/and14122001/OpenCognitiveOS/actions/runs/45915089033，15/15 jobs success。尚未合併，M5 Max 實機未驗證。
## 研究依據
- https://openai.com/index/navier-stokes-solution/ ：官方自述約萬個 agents，88h 推導與17h形式化；分組、人工資源重配及跨組整合。非無治理自治，未證明等算力效率。
- https://www.dwarkesh.com/p/noam-brown ：2026-09-17 原始訪談；平行推理、上下文分叉、通訊成本。基礎模型貢獻比例是研究者判斷，非消融數據。企業完全對齊與零摩擦不作假設。
## 決策
治理權威仍是 ExecutionAuthority；通信圖允許循環，執行依賴 DAG 不允許循環。任務、租約、權限、預算、外部效果及終態只由權威提交。Agent/Hermes 不得直寫 SQLite 或自行派工、外傳、執行工具。消息不是授權；所有內容視為不可信。先單 Agent 延長推理，僅可分解且預期收益高的工作才平行化。
## 契約（候選，未實作）
MessageEnvelope: schema_version, message_id, project_id, repo_id, task_key, execution_id, epoch, sender_id, recipient_group, sequence, causation_id, correlation_id, kind, payload_hash, payload_ref, data_class, created_at_utc, expires_at_utc. 類型 hypothesis/evidence/counterexample/proposal/request；無 command 或授權類型。權威檢查身份、群組內作用域、租約、容量、資料分類、去重與 TTL；拒絕跨項目、過期或重放，記錄拒絕證據。傳遞 at-least-once，接收者去重；不承諾 exactly-once。
ContextSnapshot: snapshot_id, parent_id, task_key, base_sha, acceptance_hash, policy_hash, evidence_refs, content_hash, model_id, tokenizer_version, budget_reservation_id。不可變、內容尋址；分叉繼承限制交集與剩餘預算，禁止複製憑證或未批准資料。合併輸出是候選 evidence/patch，保留來源、反證、衝突，不變更 acceptance_hash；獨立驗證後才發布。
TopologyProposal: graph_version, task_key, member_ids, typed_edges, reason, reservation_id, deadline。權威 CAS 批准、撤銷；拓撲只是訊息路由，不能授予工具或寫入。任務取消級聯、停止派送、釋放槽位；不明外部效果先對帳。
ComputeReservation: parent_budget_id, input/output_tokens, wall_time, inference_slots, message_bytes/count, max_cost_usd。原子保留與結算，重試/分叉/合併不增額；用量未知保守阻止新增工作。雲端逐任務批准，額外費用零，不繞過配額。
## 實施順序與驗收
M2 保留 S21–30；先真實 sandbox、工具代理、網路拒絕、只讀獨立驗收器。M3 在現有 adapter 規格之後新增 scoped persisted message bus、共用模型權重與隔離上下文；初始 2 worker/1 inference，1/2/4/8 logical agent 僅 lab 候選。M4 雲端治理不變。M5 增加 fork/merge、動態拓撲、evidence graph、預算重分配；M6 消融及回滾。既有 S01–60 不重新編號；下列新增 S61–72 全部 Pending。
- S61 身份偽造、過期 epoch、跨項目消息拒絕。
- S62 重放、亂序、重啟去重，不丟失已持久化消息。
- S63 洪泛、TTL、背壓、循環通訊停止；不耗盡治理槽位。
- S64 fork 共享父預算，併發保留不得超額；未知用量 fail closed。
- S65 fork 隔離、敏感內容不複製；合併證據可追溯。
- S66 多分支同檔寫衝突、base SHA 變更拒絕；不可覆蓋驗收規格。
- S67 動態拓撲 CAS、撤銷及取消級聯；等待不占推理槽位。
- S68 消息誘導越權、串謀假證據、共同錯誤與未授權外傳拒絕；無真實攻擊外部系統。
- S69 模型切換保留版本/語境及政策，不自動上雲。
- S70 同模型同任務等 token 預算比較單 agent、長思考、獨立採樣、階層、平面、動態分叉；另做固定 wall-clock 試驗，記錄實際 token/compute、KV、RAM、延遲、通信比率、成功率與失敗。
- S71 獨立固定驗收集、重複種子、盲測、防測試污染；模型不同時另做因子分析。
- S72 無收益/安全回歸則不升級；版本化配置、回滾、重啟、七天耐久。
## 推廣門檻
先以模型能力及單 Agent 基準選擇。預先登記任務集、種子、總 token/時間預算、成功條件；至少30個配對任務作初始研究候選，報告區間與原始數據，不足以證明普遍有效。安全測試不得退步；動態協作僅在可驗證成果/成本改善且符合資源上限時升級，否則單 Agent。硬體吞吐、實際加速和最佳 agent 數未知，不宣稱128GB可同時推理8個。
## 變更限制
本 ADR 僅工程設計，不代表已開發 S61–72。不得新建 repo、寫 main、付費 API/Runner 或提前將 M2–M6 部署生產。後續實作沿此分支／PR 依賴基線，保留獨立驗收證據。

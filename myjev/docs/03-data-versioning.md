# MyJev · 03 数据、版本与发布契约（SQLite 底座）

版本 3.1 · 两进程（A 服务/B 管理）共享一个 SQLite 库文件，本档定义表结构、写权分离与发布热加载的数据面（协议时序见 01 §3）。现状 JSON 基线 → 迁移差异登记 05。

## 1. 布局
```
data/
├─ myjev.db                      # SQLite WAL；busy_timeout=5000
├─ artifacts/<task>/<version>/   # model.pkl + meta.json（阈值组/硬规则集快照随版本固化）
├─ datasets/<id>.jsonl           # 注册数据集（附录 B/C 契约）
├─ runtime.pid / admin.pid / *.log
└─ legacy/                       # 迁移原件只读
```
发布工件必须**自包含**：artifacts 目录内的 meta.json 固化 feature_digest、threshold_set、rules 快照——A 加载不依赖 B 进程状态，保证回退可重放。

## 2. 表（schema_version=2）

| 表 | 关键列 | 写权 |
|---|---|---|
| `meta(key,value)` | schema_version、**publish_epoch**（全局单调） | B（epoch）；A 不写 |
| `tasks(id,kind,title,model_kind,active_version)` | active 指针 | B |
| `model_versions(task_id,version,status,created,note,metrics_json,artifact_path,artifact_sha256)` | (task,kind) 命名空间 | B |
| `buffer_samples(id,task_id,src,payload_json,included,created)` | synthetic 写入即 included=0 | B |
| `datasets(id,name,kind,task_id,path,sha256,row_count,fields_ver,provenance)` | 导入即校验 | B |
| `decisions(decision_id,ts,task_id,model_version,publish_epoch,fs_digest,raw_json,calibrated_json,final_json,latency_ms,exploration)` | 触发器禁 UPDATE/DELETE | **A** |
| `access_log(seq,ts,decision_id,session_key,event,ttl_left,detail_json,api_key_seen)` | event ∈ compute/hit/invalidate/fallback/rule_forced/**reload(ok|failed)** | **A** |
| `publish_events(id,ts,task_id,from_ver,to_ver,actor,epoch_after,note)` | 发布流水 | B |
| `bench_runs(id,ts,task_id,dataset_id,providers_json,sample_n,results_json)` | | B |

## 3. 写权分离（双进程唯一冲突面，定死规则）
1. A 只写 decisions/access_log；B 只写其余表；**任何越表 = 缺陷**（集成回归断言）。
2. A 读 tasks/model_versions 仅在 epoch 变化时（一致性读，无需锁）。
3. epoch bump 与 active 切换同事务（`BEGIN IMMEDIATE`），A 只会观察到 (epoch, active) 成对新值——不存在"纪元跳了版本没换"的中间态。
4. WAL + synchronous=FULL（审计持久性优先于吞吐）；checkpoint 交给 SQLite 自动。

## 4. 审计语义（两层，定稿）
- **决策层 decisions**：每次真计算恰一条，含当时 publish_epoch ⇒ "这条决策是哪个模型版本做的"永远可答；不可变。
- **执行层 access_log**：每次 /v1/systemone 调用恰一条事件；hit 行回链原 decision_id；reload 事件为热加载留痕（发布是否生效的数据源）。
- 报表口径：模型质量按 decisions；运行态按 access_log；发布链路按 publish_events ⋈ reload 事件。
- 完整性：周快照 `VACUUM INTO` 导出 + sha256 回写 meta.integrity_ledger（append 语义 JSON 数组）。

## 5. 版本/发布语义
train/finetune 产版本（finetune 不自动发布）；publish=B 单事务（校验 sha256→换 active→bump epoch→publish_events 留痕）；紧急回滚=对旧版再 publish。**缓存与新版本的关系**：会话缓存的决策不随发布自动作废，但 decisions 里已固化旧版本；epoch 切换后新计算自然用新版（无跨版本混用）。发布语义、伪标签纪律不变。

## 6. 备份/迁移/重置
备份停写或 `VACUUM INTO` 快照 + 拷 data/；迁移脚本 `scripts/migrate_to_sqlite.py`（旧 index.json/buffer/decision_log 哈希链校验后入表，epoch 初始化为迁移时刻序号）；重置=删 db+artifacts+upload-* 数据集（内置注册表行保留），起服重建。

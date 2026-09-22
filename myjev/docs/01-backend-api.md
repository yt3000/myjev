# MyJev · 01 API 与契约（服务后台 A / 管理后台 B / 发布接缝 D）

版本 3.1。通用约定：JSON/UTF-8；错误封套 `{"error":{"code","message"}}`；时间 Unix 秒；无鉴权，A/B 均仅绑定 127.0.0.1（04 §3 安全边界）。`X-API-Key` 仅记入 access_log.detail 不验证。

## 1. 交付物 A：服务后台 API（:8091，无界面）

| 端点 | 用途 | 关键约束 |
|---|---|---|
| `POST /v1/systemone` | Jev 兼容决策（附录 A 超集）：`{task, context, questions[], audit?}` → `answers[]` + `pdp{}`(access 任务) + `meta{}` | 未知 noul 头逐题 `error`（HTTP 200）；options 子集重归一；每次调用 decisions+1（计算时）或回链 hit（命中时），access_log 恒 +1 |
| `GET /health` | 存活 + 已加载版本摘要 | `{ok, loaded:{task:version…}, version_epoch, ts}` |
| `GET /api/version` | 当前生效的发布纪元与工件摘要 | 供 smoke/外部审计核对"我打到的是哪个模型" |
| CORS | 允许 `null` 源与 `http://127.0.0.1:*`（C 以 file:// 双击使用的前提） | 仅 A 开放；B 不开 |

缓存语义（03 §4）：会话决策缓存 TTL 命中 ⇒ 只记 hit 事件，不重算；context 漂移/全局事件/过期 ⇒ 重算。

## 2. 交付物 B：管理后台 API（:8090，含控制台页面）

### 2.1 配置与任务（R1）
- `GET /api/tasks` 快照（含 model_kind、primitives/options、versions[]、buffer 计数）
- `PUT /api/tasks/{id}/model-kind`：gbm-multitask 可用；logit-probe 预留（训练/发布 501）
- `GET/PUT /api/config/thresholds/{id}`：阈值组查看与克隆修改（新 id 版本化，禁止原地改生效组）
- `GET/PUT /api/config/rules/{id}`：硬规则集（denylist/whitelist/floor/break-glass）同上，**发布后 A 经 epoch 生效**
- `GET /api/model-kinds`

### 2.2 训练·微调·检测（R1）
- `POST /api/tasks/{id}/train|finetune|evaluate`：语义同 v3.0（finetune 不自动发布；仅取 included=1；evaluate=门禁+PSI+扰动）
- `POST /api/feedback` `GET /api/tasks/{id}/buffer`（src 写入侧纪律，synthetic ⇒ included=0）

### 2.3 数据集与比对（R3）
- `POST/GET/DELETE /api/datasets*`、`preview`（契约=附录 B/C；upload-* 可删，内置与注册基线不可删）
- `POST /api/bench {task, dataset_id|builtin, providers[], sample_n?}` → BenchRow/radar（mode 徽章强制）；`GET /api/bench/baselines`；每次运行落 bench_runs
- `GET /api/audit/decisions`、`GET /api/audit/access-stats`（读 A 写的两张表；控制台"审计"视图数据源）

### 2.4 发布（R1 → 驱动 D）
- `POST /api/publish {task, version}`：单事务——校验工件 sha256 → `tasks.active_version` 切换 → `meta.publish_epoch += 1` → publish_events 留痕。**幂等**：重复发布同版本仅 bump 一次。
- `GET /api/publish/status`：各任务 active 版本 + A 侧回执的 `loaded_epoch`（A 每次加载后写 access_log 事件 `reload`，B 聚合展示）→ **B 界面能看到"发布已生效/未生效/失败回退"**。

## 3. 接缝 D：发布热加载契约（两进程拆分的核心新增件）

```
B: publish ─▶ db.meta.publish_epoch=N+1
A: 每 2s 轮询 epoch（空闲 tick）
   epoch 变化 ⇒ 读 tasks/model_versions(active, artifact_sha256)
   ⇒ 校验文件 sha256 ⇒ 后台线程 load(pickle→bundle+cals+thresholds+rules)
     ├─ 成功: 双缓冲原子换入, 写事件 reload(ok, N+1)
     └─ 失败: 保留旧版继续服务, 写事件 reload(failed, N+1, err), 旧 epoch 视作 lag
```
- **时序保证**：换入瞬间的在途请求继续用旧实例（引用计数/整体对象替换，不出现半新半旧）。
- **失败矩阵**：文件缺失/摘要不符/反序列化异常/头集不完整 → 全部"不切换+留痕+健康页可见 lag"，绝不让 A 进入无模型状态。
- **紧急回滚** = B 对旧版本再 publish（epoch 再 bump），A 按同流程加载。
- 首启：A 启动时同步加载一次（无旧版可保 ⇒ 失败即拒绝启动并报错，不空转）。
- 轮询间隔/仅 loopback/单实例约束写入配置 `runtime.yaml`（示意，实现为常量亦可）。

## 4. 工程测试资产（F，本轮文档定稿、实现入重构）
| 资产 | 断言 | 挂钩 |
|---|---|---|
| `scripts/tests/smoke_runtime.py` | 以纯外部进程身份只调 A：三任务三原语字段契约（附录 A 超集验证：忽略 pdp/band 仍可用）、401 不存在（无鉴权声明）、epoch 一致性（/api/version vs B publish 回执） | D 契约验收 |
| `scripts/tests/test_integration.py` | B train→finetune→publish→A 在 3s 内 loaded 回执→C 场景请求→decisions/access_log 行数核对 | A/B/C 全链路 |
| 页面 `node --check`（门禁脚本 `scripts/check_ui.js`） | A/B/C 三处前端内联 JS 语法 | 防白屏复发 |
| 契约快照 | `GET /openapi.json`（A、B 各一份）diff 基线 | 接口防漂移 |

## 5. 数据对象
TaskSnapshot / BenchRow / metrics 三对象定义沿用 v3.0 §4，不变；新增：`PublishStatus {task, active_version, publish_epoch, loaded_epoch, lag, last_error}`。

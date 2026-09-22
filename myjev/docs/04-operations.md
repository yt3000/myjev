# MyJev · 04 运维与故障排查（含交付手册要点）

版本 3.1 · 两进程拓扑（A 服务后台 :8091 / B 管理后台 :8090）+ 独立静态测试 App C。本档即交付文档包 G 的骨架（部署手册），API curl 示例为附录一，OpenAPI 在线文档由 FastAPI `/docs` 自动生成（A、B 各一份）。

## 1. 命令矩阵

| 操作 | Windows | Git Bash |
|---|---|---|
| 全部启动（守护，推荐） | `start.bat serve-all` | `./start.sh serve-all` |
| 单独启动 A / B | `start.bat serve-runtime` / `serve-admin` | 同参数 |
| 全部停止（pid 优先+端口兜底，幂等） | `start.bat stop-all` | `./start.sh stop-all` |
| 状态（两进程 pid/端口/epoch 回执一行显示） | `start.bat status` | `./start.sh status` |
| 前台调试（单进程，Ctrl+C 即停） | `start.bat serve-admin`（无 --daemon） | 同左 |
| 测试 App | 双击 `myjev/app/index.html`（无需启动） | 同左 |

实现约定：`serve.py` 收敛为统一入口（`--role runtime|admin --daemon|stop|status`）；pid/log 分文件（runtime.pid/admin.log…）；`serve-all` = 先 A 后 B 顺序拉起并各自健康确认；`stop-all` 反序；两者均幂等。启动前置：venv 依赖（requirements.txt），端口预检失败退 3 并打印占用 pid。

## 2. 端口与文件表

| 项 | 值 |
|---|---|
| A 服务后台 | 127.0.0.1:8091（仅 loopback；CORS 允许 null 与 127.0.0.1 源） |
| B 管理后台 | 127.0.0.1:8090（仅 loopback；无 CORS） |
| 共享数据 | `data/myjev.db`、`data/artifacts/`、`data/datasets/` |
| 运行文件 | runtime.pid / admin.pid / runtime.log / admin.log |

## 3. 安全边界（"不鉴权"裁决的成立条件，必须满足）
1. A/B **强制绑定 127.0.0.1**，配置不提供改绑局域网/0.0.0.0 的开关（改绑=推翻裁决，需重开鉴权与非目标评审）；
2. CORS 白名单仅本机源与 null；`X-API-Key` 记录不验证（留审计线索）；
3. 补偿控制：decisions 不可变审计 + access_log 全事件留痕 ⇒ 即便本机进程误调用也可事后追溯；
4. 训练/发布等高危操作只存在于 B（8090），A 永远无写模型权限——误暴露 A 也不产生破坏面。

## 4. 发布-生效运维视图（日常只盯这三处）
- B 总览 `epochChip`：绿（loaded_epoch==publish_epoch）/黄（滞后 ≤3 个轮询周期）/红（reload failed，悬停看 last_error）；
- `runtime.log` 的 reload 行；红色时标准处置：确认 artifacts 文件在位且 sha256 一致 → B 回滚上一版 → 报缺陷（附 last_error）。

## 5. 故障排查表

| 症状 | 根因 | 处置 |
|---|---|---|
| C 页面黄条"服务未运行" | A 未起/崩 | `start.bat status` → `serve-all`；看 runtime.log |
| 管理页红条诊断条 | 前端脚本异常（语法类已被 node --check 门禁拦截，此处应为逻辑错） | 收集堆栈报缺陷，禁止静默 |
| epochChip 持续红 | 工件损坏/摘要不符 | §4 标准处置 |
| 端口占用退 3 | 残留实例 | `stop-all`；仍占用则按打印 pid 处理 |
| 决策 API 501 | 任务挂了 logit-probe 等预留模型族 | 换 gbm-multitask 或等 M5 |
| 发布后旧版本仍在服务且无红标 | A 轮询间隔内正常滞后（≤2s） | >10s 再判故障 |
| SQLite `database is locked` | 违反写权分离（代码缺陷）或迁移期双写 | 停 B 重放；集成回归断言表写权 |
| C 的 fetch 被 CORS 拦截 | A 未起/CORS 配置漂移 | `curl :8091/health` 定位 |
| 首次启动等待 1–2 分钟 | 自动初始训练 | 正常，看 admin.log 进度行 |

## 6. 备份 / 重置 / 升级
备份：`stop-all` → 拷 `data/`；或在线 `VACUUM INTO`。重置演示态：删 db+artifacts+upload-* 数据集 → `serve-all` 自动重建。升级（重构项目交付后）：版本号看 `/api/version` 与 B 关于页；schema 变更由 db.py 迁移钩子自动执行并在 meta.integrity_ledger 留行。

## 附录一 · API 示例（curl，随手册交付）
三原语各一例（choice 全枚举 / noul 单头 / score），access 任务含 `pdp{}` 阅读指引、无 `pdp{}` 的纯 Jev 消费示例各一条——内容以 `scripts/tests/smoke_runtime.py` 通过为准（文档-代码同源断言）。

# MyJev · 05 文档-代码差异清单 v2（重构项目输入）

版本 3.1 · 依据 v3.1 交付物拆解（A 服务后台 / B 管理后台 / C 测试 App + D 发布契约 + E 编排 + F 测试资产 + G 文档包）重登记。当前代码 = v1.0.0 单进程平台版（文档称"基线"）。每项：现状 → 目标（落点）→ 量级 → 验收钩子（F 资产建成后统一挂 smoke/integration）。

| # | 差异项 | 现状 | 目标（落点） | 量级 | 验收钩子 |
|---|---|---|---|---|---|
| G1 | **双进程拆分** | 单 app.py 同时承载推理+管理+页面 | `create_runtime_app()`(:8091，无界面)/`create_admin_app()`(:8090)；入口 `serve.py --role`（00 §2、04 §1） | L | integration 断言 A 无任何管理路由、B 无 /v1/systemone |
| G2 | **发布热加载契约（D）** | 发布=同进程内存 active 切换（无跨进程语义） | publish_epoch + A 轮询 + sha256 校验 + 双缓冲换入 + reload 事件 + epochChip 三态（01 §3、03 §3-5） | M | publish 后 ≤3s A /api/version 回执一致；投毒工件（改摘要）→ 红标不切换 |
| G3 | **SQLite 底座（含写权分离）** | index.json/buffer jsonl/哈希链 jsonl/pickle 裸盘 | 03 §2 全表 + 触发器不可变 + 写权断言 + `VACUUM INTO` 备份 + 迁移脚本 | L | 迁移条数一致；integration 违权写测试红 |
| G4 | decisions/access_log 两层审计 | 单层哈希链；缓存命中零痕迹 | 03 §4；hit 回链 decision_id；api_key_seen 记录 | M | 同会话二次调用：decisions 不增、access_log+1 hit |
| G5 | 零 CDN 前端 + SVG + 双保险横幅（B 控制台） | Chart.js CDN；diffCard 语法已修（白屏根因①除） | 02 §1-2（六视图：测试应用迁出、新增发布状态表、配置抽屉） | M | 断网点检六视图；node --check 门禁脚本常跑 |
| G6 | **测试 App C（新交付物）** | 测试应用在控制台视图内、且可回灌 buffer | 独立 `myjev/app/index.html`，只调 8091；两屏 + 批量跑批 + 导出；**无回灌**（飞轮非目标，见下"待复核"） | M | integration 断言 C 页面源码不含 8090 引用；file:// 直开可用 |
| G7 | 编排 E（serve-all/stop-all/status/pid 分文件） | 单进程 --daemon 草稿 + 端口清理 stop 脚本 | 04 §1 全命令（草稿 serve.py 可复用改造） | S | 关终端后 status 两绿；stop-all 幂等 |
| G8 | 数据集注册与 bench dataset_id | bench 仅内置合成集 | 01 §2.3 + 附录 B/C 契约、导入即校验 | M | 导入 10 行样例→preview→bench 指定运行落 bench_runs |
| G9 | model_kind 可插拔 | 硬编码 gbm 头 | 01 §2.1 kinds + 501 语义 + (task,kind) 命名空间 | M | 挂 logit-probe 训练→501；gbm 全回归 |
| G10 | 配置管理（阈值/硬规则版本化） | 代码常量（ThresholdSet/HardRuleEngine 硬编码） | 01 §2.1 GET/PUT 克隆式配置 + 随版本快照固化进 artifacts（03 §1） | M | 克隆新阈值组→发布→A 生效→C 决策随动 |
| G11 | 工程测试资产 F | curl 手工冒烟（历史） | 01 §4 四件（smoke/integration/check_ui/openapi diff 基线） | S | 全部纳入 start 前可选 precheck |
| G12 | buffer src 写入侧纪律 | 无 src 约定 | 03 §2 included=0 规则 | S | 注入 synthetic → finetune skipped 文案 |
| G13 | 交付文档包 G | 本套 docs（已成形） | 04 定稿为手册 + curl 附录脚本化生成 + OpenAPI /docs 双份 | S | 新人按手册 30 分钟完成三件套冷启动演练 |

## 实施顺序（依赖驱动）
G1+G7（拓扑先行，端口语义立住）⇒ G3+G4（存储一体）⇒ G2（接缝）⇒ G12→G10→G9→G8（管理面功能）⇒ G5+G6（前端两交付物）⇒ G11→G13（资产与手册收口）。

## 待复核（本轮重拆解的衍生裁决，默认生效、可单点推翻）
1. **C 无反馈回灌**（G6）：因"只调 A"+飞轮延后二选一所得；若坚持回灌 ⇒ 需开 C→B 通道或 A 转发端点，请裁决。
2. **B 控制台撤"测试应用"视图**（G5）：职责归 C；若希望管理后台保留演示入口，可加 iframe 但不加逻辑。
3. 原"独立 API 客户端库撤销、curl 示例入手册"已在 02/04 落实。

## 存量能力（零改动继承）
三原语推理与 PDP 合成、校准/门禁/扰动/PSI 评测、微调 Δ 报告、三方 bench 数值层（与附录 B/C 对表）、start/stop 脚本、node --check 实践。

---

## 修订记录（追加，不改上文）

**2026-09-22 · v3.1 重构实施完成（G1–G13）**
- 已实施：G1 双进程（runtime_app/admin_app/serve --role）✅；G2 热加载契约（epoch 轮询+双缓冲+
  force 重发布+reload 事件）✅；G3 SQLite 底座（db.py+authorizer 写权+迁移脚本，旧数据 4 版本/8 反馈/
  3 决策已迁入，原件 data/legacy/）✅；G4 两层审计（decisions 触发器不可变 + access_log 事件）✅；
  G5 零 CDN 控制台六视图（check_ui 过）✅；G6 测试 App 独立页（myjev/app/）✅；G7 编排
  serve-all/stop-all/status ✅；G8 数据集导入+bench dataset_id ✅；G9 model_kind+501 ✅；
  G10 阈值/规则克隆配置随发布快照 ✅；G11 smoke(15断言)/integration(8)/check_ui ✅；G12 src 写入侧纪律 ✅；
  G13 README 重写为本状态 ✅。
- 验证：集成 8/8、内核单测 12/12、契约冒烟 15/15、E2E（发布→epoch 4→A 4s 热加载→缓存 hit 回链）通过。
- 遗留转下轮：监控告警条、真实日志接入（M1）、logit-probe 实现（M5）、jev-102 原始集获取（附录 B）。

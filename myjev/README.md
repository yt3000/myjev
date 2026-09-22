# MyJev —— 零信任 System One 复现 · 双后台平台（v3.1 已实施）

按《docs/00–05 + 需求附件 A/B/C》（v3.1 冻结稿）与《零信任SystemOne复现模型规格》v0.4 实现的运行平台：
Jev 式三原语决策（Choice/Score/Noul，类型化 + 校准概率、不生成文本）+ 零信任策略层
（硬规则优先/fail-closed/能力上限）+ 不可变决策审计。当前数据为合成攻击剧本（M1 飞轮前）。

## 交付物与进程拓扑

```
A 服务后台  myjev/runtime_app.py  127.0.0.1:8091  纯推理 API（/v1/systemone，Jev 兼容超集）
B 管理后台  myjev/admin_app.py    127.0.0.1:8090  控制台+配置/微调/评估/数据集/发布
C 测试 App  myjev/app/index.html  双击打开         只调 A（访问台 + 工单流程两屏）
D 发布契约  epoch 轮询热加载（01 §3：校验→双缓冲换入→失败回退留痕）
E 编排      myjev/serve.py        serve-all/stop-all/status（pid+log+双路清理）
F 测试资产  scripts/              smoke_runtime / test_integration / check_ui.js / migrate
G 文档      docs/                 00–05 + 附录 A/B/C（冻结基线，附录只增不改）
共享存储    data/myjev.db（SQLite WAL，写权分离）+ data/artifacts/ + data/datasets/
```

## 快速开始

```bat
start.bat setup            :: 首次：venv + 依赖
start.bat serve-all        :: 守护启动 A+B（首次自动训练三模型，约 1–2 分钟）
start.bat status           :: 两进程状态 + epoch 回执
start.bat stop-all         :: 幂等停止（pid 优先、端口兜底）
:: 管理控制台 http://127.0.0.1:8090 ・ 测试 App 双击 myjev\app\index.html
start.bat test             :: 内核 12 项 + 集成 8 项
start.bat check            :: 依赖自检（node scripts/check_ui.js 需本机 node）
```

Git Bash 等价：`./start.sh serve-all|stop-all|status|test`。旧单机演示保留为 `start.bat demo`
（M2 管线，写 artifacts/，与服务态互不影响）。

## 日常动线（对应 R1–R4）

1. **R1 管理**：控制台「任务与模型」看版本/切 model_kind →「训练与微调」train/finetune（微调
   产新版本**不自动发布**）→「效果评估」跑 §11 门禁 → 发布按钮原子切换（总览发布状态表实时
   显示 A 的 epoch 回执：ok/lagging/failed）；「审计与发布」页克隆修改阈值/硬规则 → 重发布生效。
2. **R2 服务**：`POST :8091/v1/systemone`（附录 A 超集：choice/noul/score + 扩展 `pdp{}`）。
   每次调用落两层审计：decisions（不可变，触发器强制）+ access_log（compute/hit/reload 事件）。
3. **R3 对比**：「能力对比」选数据集（内置或导入 upload-*）与参与方 → SVG 雷达+三联柱状+明细，
   mode 徽章强制（live 实测 / 宣称回放 / 实测回放，禁止跨口径叙述）。基线数值逐条回指附录 B/C。
4. **R4 应用**：C 页面「零信任访问台」8 场景 + 「工单流程」单张/跑批；历史可导出 JSON 送人工
   复核（本轮无回灌——裁决：反馈通道属数据飞轮，延后）。

## v3.1 实施验证记录（2026-09-22）

- 集成 8 项 + 内核 12 项单测全绿；写权分离（B 写 decisions 被 authorizer 拒绝）、
  decisions 触发器不可变、同版本带配置重发布强制 epoch bump、A 4s 内热加载回执——均已实测。
- 契约冒烟 15 断言全过（纯 Jev 客户端视角 + A/B 路由边界）。
- 旧平台数据完整迁入：4 版本（含微调版）、8 反馈样本、3 决策入 SQLite，原件存 data/legacy/。
- 两前端页面 `node --check` 通过（check_ui.js 门禁）。
- 控制台首屏/发布链 E2E 实测：publish→epoch 4→loaded 4；同会话二次调用 hit 且 decision_id 回链一致。

## 已知边界（诚实清单）

1. 数据为合成剧本：指标证明管线正确，不证明域性能；M1 接入真实日志后重跑门禁才算数。
2. 安全边界=仅绑定 127.0.0.1 + 不鉴权（裁决 R2）；跨机部署前必须重开鉴权评审（附录 A 预留位）。
3. Jev/OpenJev 对比为公开资料回放（mode 徽章），非本次头对头；102 行原始集待获取（附录 B）。
4. logit-probe 模型族（M5）为 501 预留；文本句向量头（sensitive_policy_hit 语义版）未接入。
5. 数据飞轮导入、监控告警、API-Key 验证：按 00 裁决本轮不交付。

## 模块 ↔ 规格映射（算法内核层）

| 规格 | 模块 | | 规格 | 模块 |
|---|---|---|---|---|
| §2 决策契约 | `contract.py` | | §6.1 校准 | `calibration.py` |
| §3 特征/快照 | `features.py` | | §6.2/6.3 阈值·能力上限 | `thresholds.py` |
| §4 标签/剧本 | `data.py` | | §7 PDP 合成 | `pdp.py` |
| §5 多任务头 | `models.py` | | §9 审计 | `db.py`(decisions) + 旧 `audit.py`(legacy) |
| §11 门禁/扰动 | `tasks.py::_metrics/_perturbation` | | §13 对比基线 | `providers.py`（回指附录 B/C） |

平台层：`db.py`(G2/3/4) `tasks.py`(注册/发布) `runtime_app.py`/`admin_app.py`(A/B)
`serve.py`(E) `scripts/`(F) `docs/`(G)。

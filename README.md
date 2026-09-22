# MyJev

零信任场景下的 [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) 式 **System One 决策模型**复现平台：不生成文本、只输出**类型化 + 校准概率的结构化决策**（Choice / Score / Noul 三原语），以前后端分离的三交付物形态提供可运行的"智能 if 语句"服务。

> Jev is not open source — MyJev answers: can a self-hosted discriminative model reproduce the *output contract* of System One and survive zero-trust audit requirements?
> 复现依据：TypeSafe Jev 官方博客 / OpenRouter 上架资料 / OpenJev（TheoLeeCJ WebGPU 探针）与国内外评测报告，全部口径固化在 [docs/appendix-A/B/C](myjev/docs/)。

## 它是什么

| 交付物 | 说明 |
|---|---|
| **A · 服务后台** `:8091` | 纯 API 进程。`POST /v1/systemone`（Jev 兼容**超集**：三原语字段语义不变，扩展 `pdp{}` 策略合成块）；模型工件热加载（发布后 ≤3 个轮询周期生效，失败自动回退旧版）；每次调用落**不可变决策审计** + 执行痕迹 |
| **B · 管理后台** `:8090` | 模型资产控制台：任务↔模型绑定、buffer 微调（前后指标 Δ 对照）、§11 效果门禁、数据集导入、发布/回滚（epoch 回执三色灯）、三方能力对比（自绘 SVG 雷达+三联柱状，零 CDN） |
| **C · 测试 App** | 独立单页应用（双击即用、断网可用）：零信任访问台 8 场景 + 工单路由/紧急度流程，只允许消费 A 的公开 API——用第三方视角实证接口兼容性 |

**算法主线**：LightGBM 多任务判别头（3 类决策 + 8 个 Noul 布尔头 + 序数风险分 + 认证因子头）→ 温度缩放/isotonic 逐头校准 → 双阈值+灰区 → 确定性 PDP（硬规则优先、模型只升不降、fail-closed、能力上限防爆炸半径）。SQLite 底座以 authorizer 实现**双进程写权分离**，decisions 表触发器强制不可变。

## 快速开始

```bat
git clone https://github.com/yt3000/myjev.git && cd myjev/myjev
start.bat setup        :: 创建 venv + 依赖（Python 3.10+，需 numpy/scipy/sklearn/lightgbm/fastapi/uvicorn）
start.bat serve-all    :: 守护启动 A+B（首次自动训练三个模型，约 1–2 分钟）
start.bat status       :: 运行状态：两进程 pid + epoch 回执
```

打开 `http://127.0.0.1:8090`（管理后台）· 双击 `myjev/app/index.html`（测试 App）。
Linux/macOS：`./start.sh serve-all`。停止：`start.bat stop-all`。

## 一次决策长什么样

```bash
curl -X POST http://127.0.0.1:8091/v1/systemone -H "Content-Type: application/json" -d '{
  "task": "access-decision",
  "context": {"principal":"bob","features":{"c_device_switch":1,"s_fail_logins_1h":12,"g_geo_anomaly":1,"h_risk_ewma":0.7,"g_ip_reputation":0.25}},
  "questions": [
    {"id":"a","type":"choice","options":["allow","step_up","deny"]},
    {"id":"n","type":"noul","question":"account_takeover"},
    {"id":"s","type":"score"} ]}'
```

```json
{ "answers": [
    {"id":"a","type":"choice","choice":"deny","probabilities":{"allow":0.0,"step_up":0.001,"deny":0.9987},"confidence":0.9987},
    {"id":"n","type":"noul","value":0.9403},
    {"id":"s","type":"score","score":0.895,"band":"critical"} ],
  "pdp": { "final_action":"deny","auth":"none","grant":"read_only_ttl60",
           "hard_rules":[],"fallback":null,"decision_id":"d-…" } }
```

三个内置任务：`access-decision`（零信任访问，全原语）、`support-routing`（工单路由，Choice）、`urgency-score`（紧急度，Score）——演示了"每类任务挂不同处理模型"的服务化形态。

## 与 Jev / OpenJev 的对比纪律

管理后台"能力对比"页支持对指定测试集运行三方对比，全部基线数值可回指需求附件：

| | Jev（官方宣称） | OpenJev（第三方实测） | MyJev（本仓库，live 实测） |
|---|---|---|---|
| 决策一致率 | 88.3%（公开案例）/ 67.8%（真实业务流） | 4B 零样本 84.5% | 域内门禁指标（不跨域比分数） |
| 概率校准 | RLCD（宣称） | 无（候选间条件概率） | 温度/isotonic，**逐头 ECE ≤0.02 硬门禁** |
| 扰动鲁棒 | 未公开 | 顺序反转翻转 **27.8%** | 固定 schema 结构性免疫（顺序 0%，无关注入实测 ~6%） |

规则：图表每行带 mode 徽章（`live·实测` / `replay·宣称` / `replay·实测回放`），非 live 行不参与胜负叙述——这是 OpenJev 事故数据（未校准概率 + 顺序敏感 = 高风险路由隐患）给出的教训。

## 质量证据（可复跑）

```bash
start.bat test                                  # 集成 8/8 + 内核单测 12/12
.venv/Scripts/python scripts/smoke_runtime.py   # 契约冒烟 15 断言（A 在线时）
node scripts/check_ui.js                        # 双前端语法门禁（白屏事故后立的规矩）
```

## 仓库地图

```
myjev/                 平台代码
 ├─ myjev/             内核(contract/features/models/calibration/thresholds/pdp/db)
 │                    + 引擎(tasks) + 双进程(runtime_app/admin_app) + 编排(serve)
 ├─ myjev/web/         交付物 B 控制台 ・ myjev/app/ 交付物 C 测试App
 ├─ scripts/           迁移/冒烟/集成/check_ui 测试资产
 ├─ docs/              00–05 设计文档 + 需求附件 A/B/C（Jev 接口与两方数据集契约）
 └─ start.bat|sh ・ stop.bat|sh ・ README.md  使用说明
零信任SystemOne复现模型规格-v0.1.md   算法内核规格（正文 v0.4，冻结基线）
Jev-OpenJev-模型能力对比.html          公开资料的三方对比图表页（离线打开）
```

## 诚实的边界

1. **数据是合成的**：内置攻击剧本（撞库/ATO/伪造姿态/外传/探针等 10 类）证明管线正确，不证明域性能——真实日志飞轮（M1）在路线图；
2. **无鉴权是裁决而非疏忽**：服务仅绑定 `127.0.0.1`；跨机部署前必须重开鉴权评审；
3. **Jev/OpenJev 列是资料回放**，非本次头对头；102 行原始评测集待获取（附录 B 已定导入契约）；
4. logit-probe 模型族（4B 免解码探针，对齐 Jev 的另一条路线）为 501 预留实现位。

## Roadmap

M1 真实日志接入与反馈飞轮 → M3 影子模式 → M5 免解码 Logits 探针对照（[算法规格 §13](myjev/docs/00-overview.md) 有基线）→ 监控告警 → 数据集复测升级。

*License: 未声明（默认保留所有权利），需要请提 issue。*

# 需求附件 A · Jev 访问接口契约（固化留存）

来源与置信度标注：① TypeSafe 官方博客《Introducing System One models and Jev》(typesafe.ai/blog/introducing-system-one-models-and-jev，抓取超时，内容经 OSCHINA/腾讯云/DEV 等多源交叉转述确认)；② DEV 实战指南 (dev.to/valyuai, 2026-09-17, 直接命中)；③ OpenRouter 列表（原 `typesafe/jev-1.13` 已 404，现役形态 `~typesafe/jev-latest`；另上架 Cloudflare Workers AI）。

## A.1 端点与 SDK
- 直连：`POST https://api.typesafe.ai/v1/systemone`
- SDK：Python `typesafe-sdk`；JS/TS `@typesafe-ai/sdk`；入口字段 `system_one` / `systemOne`
- 输入形态：程序状态（program state）+ 类型化问题列表；输出为类型化决策，不生成自由文本。

## A.2 三原语语义（响应契约）
| 原语 | 请求要素 | 响应字段 |
|---|---|---|
| Choice | options 枚举集 | `choice`、`probabilities`（逐选项）、`confidence` |
| Score | 有序刻度/锚点 | `score`（可落档位之间）、`probabilities`、`confidence` |
| Noul | 单一布尔问题 | 数值 = "答案为是的概率" |

## A.3 官方宣称的能力参数（引用时必须带"宣称"标注）
- 结构化输出错误 0%（by construction）；幻觉在其边界内结构性不可能
- 延迟 70–500 ms；对自回归生成提速 40–200×
- 定价：输入 ~$0.042/MTok，输出免费（"低到不值得计费"）
- 训练：RLCD（强化学习校准决策），锚定 GPT-6 Astra 与 Claude Fable 5.1 共识概率
- 闭源：权重未公开；截至归档无第三方独立复测其校准质量

## A.4 MyJev 兼容声明（R2.1 契约映射）

**定性（2026-09-22 裁决，修复 D5）：MyJev 接口是附录 A 语义的"兼容超集"**——三原语响应字段不改名不改义；`task`、`pdp{}`、`score.band`、`audit` 为扩展件，纯 Jev 客户端忽略扩展件即可完整工作（此约束即 02 §6 独立客户端 smoke 的断言依据）。
| Jev 语义 | MyJev 实现 | 兼容性 |
|---|---|---|
| system_one 请求 | `POST /v1/systemone {task, context, questions[]}` | 同构；多一个 task 路由字段（超集） |
| Choice | `{choice, probabilities, confidence}`；options 子集自动重归一 | 完全兼容 |
| Score | `{score, band, confidence}`；band 附赠（超集） | 兼容 |
| Noul | `{value, error}` 逐题作答，HTTP 恒 200 | 兼容；未知头返回 error 不炸请求 |
| 输出类型化 | 判别头概率全部过后校准（§6.1） | 我们的差异化强项：校准有实测 ECE 证据 |
| —（无对应） | `pdp{}` 扩展块（硬规则/fail-closed/权限上限） | 扩展命名空间，不侵入 Jev 原语字段 |

保留位与裁决（2026-09-22）：`X-API-Key` 本轮**只接收、记入 access_log.detail、不验证**（鉴权列非目标；做实验证归重构项 G4/G7 挂钩）。真实 Jev provider 直连保持接口形态预留，附录 B 数据未获取前 bench 的 Jev 列只用回放。

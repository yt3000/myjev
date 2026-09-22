# 需求附件 C · OpenJev 测试数据集与实测基线（固化留存）

来源：TheoLeeCJ/openjev（WebGPU 纯前端实现；METHOD.md + webgpu-demo/README，经 ic.work《OpenJev提速5.21倍》Ada Vector 2026-09-18 汇总转述）；zhihz/openjev（独立本地双语实现，README 直接命中）。用途：R3 对比 OpenJev 列与 M5 门禁基线的唯一数据源；`providers.OPENJEV_BASELINE` 逐条回指本附录。

## C.1 评测矩阵（706 行）构成
| 分区 | 行数 | 说明 |
|---|---|---|
| 原创决策用例 | 144 | OpenJev 自建 |
| WANLI 推理样本 | 256 | 公开自然语言推理数据集 |
| TypeSafe 公开案例行 | 102 | 附录 B 的公开子集（跨方锚点） |
| Every Labs 工件 | 204 | 第三方评测工件 |

## C.2 一致率（102 行子集，原生 BF16）
Qwen3-0.6B **40.7%** → MiniCPM5-2B **63.7%** → Qwen3.5-4B **84.5%**（对照官方宣称 88.3%，参照非对决）。规模敏感性结论：零样本探针必须 ≥4B。

## C.3 延迟与提速（RTX 3090 + Chrome 152，浏览器端）
| 档位（量化档） | 直接读 Logits | 自回归 JSON | 提速 |
|---|---|---|---|
| Qwen3-0.6B Q8_0 (639MB) | 0.704 s | 1.484 s | 2.11× |
| MiniCPM5-2B Q4_K_M (1.56GB) | 1.508 s | 4.193 s | 2.78× |
| Qwen3.5-4B Q4_K_M (3.01GB) | 3.271 s | 8.164 s | 2.50× |
| Qwen3.5-4B BF16 · 21 问共享上下文 | **1.023 s（0 token）** | 5.332 s（111 token） | **5.21×**，18/21 项 top-1 与逐字生成一致 |

## C.4 扰动鲁棒（各 36 用例，最高预测翻转率）
选项顺序反转 **27.8%**（10/36）· 判定准则同义改写 **25.0%**（9/36）· 混入无关上下文 **11.1%**（4/36）。→ 直接采用为 M5 探针门禁的"未去偏基线"，我方门槛：去偏后 ≤10% / ≤10% / ≤8%（[算法规格]§8.4/§11.2）。

## C.5 机制与环境限制（架构参考）
- 方法：单次前向 + 对预设 A–T 选项 token 施加等额 `logit_bias` + 取 top-20 logprobs + 候选间 softmax；跳过解码循环。
- 栈：WebGPU + Web Workers + wllama 3.6.1；GGUF 权重 HF 静态缓存；无后端/无遥测。
- 限制：2048 token 上下文截断；单次 ≤20 选项；WebKit 较 Chrome 慢近一倍；概率仅为候选间条件概率（非校准置信，项目自认）。
- zhihz 版差异：冻结 Qwen3-4B-Instruct-2507 后端；逐问重编码上下文（低效项，延迟数据不可与 TheoLeeCJ 版互用）。

## C.6 导入契约（kind=`openjev-matrix`，fields_ver=1）
706 行矩阵若离线获取（TheoLeeCJ/openjev 仓库评测工件 + 冻结结果快照），按 B.1.1 同一行格式扩展两字段：`part ∈ {scen-144, wanli-256, typesafe-102, everylabs-204}`、`pred.openjev_4b`（其公布的原样预测，用于三方对照复算）。获取前 R3 的 OpenJev 列以本附录 C.2–C.4 汇总值回放。

## C.7 更新纪律
同附录 B：只增不改，新实测以追加修订节进入并标注来源日期。

"""MyJev —— 零信任 System One 复现模型（按《零信任SystemOne复现模型规格》v0.4 实现）。

M2 阶段主线：LightGBM 多任务判别头 + 温度/isotonic 校准 + 双阈值灰区
+ 硬规则优先 PDP + 哈希链审计。本包只输出校准后的类型化决策（Choice/Score/
Noul 三原语），不生成文本。
"""
__version__ = "0.4.0"

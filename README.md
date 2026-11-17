# 海洋数据挖掘作业

浙江大学课程实验作业。研究多数据源温度查询中，编号目录与观测快照独立更新时，如何让过期的历史错误退出检查，同时保留仍有效的证据。站点、温度与工具故障由本地程序模拟。

- [项目仓库](https://github.com/YoreJ/ocean-data-mining-homework)
- [最终报告](report/正式报告.pdf)
- [实验设计与修改对话](conversations/本窗口聊天记录-2026-11-11.md)
- [动态版本实验的逐案结果](cases/versioned-recovery-benchmark.json)
- [固定版本实验的逐案结果](cases/recovery-benchmark.json)

## 动态版本实验

编号无效证据匹配编号体系、目录版本和站点编号。无记录证据同时匹配目录、观测快照和完整查询。超时允许重试，证据所需的元数据缺失时允许查询。

- 旧证据失效的 10 个条件中，版本校验全部取得温度，原固定环境检查完成 2 个。
- 旧证据仍有效的 12 个条件中，版本校验与整体清空历史均全部完成，分别执行 18 次和 22 次工具调用。
- 另有 9 个边界场景，验证超时、查询键差异和版本元数据缺失时的行为。

## 固定版本实验

在相同起点、故障顺序和脚本提案下，对比直接执行、所有失败按相同参数去重、仅确定失败按相同参数去重，以及按历史证据范围检查。共枚举 30 个压力条件，另运行 4 个边界场景。恢复阶段每种方案最多执行 4 次工具、读取 6 次提案。

| 执行前策略 | 取得温度 | 工具调用 | 执行冗余查询 |
|---|---:|---:|---:|
| 直接执行 | 15 / 30 | 106 | 60 |
| 所有失败去重 | 10 / 30 | 57 | 12 |
| 确定失败去重 | 24 / 30 | 80 | 12 |
| 按证据范围检查 | 26 / 30 | 70 | 0 |

上述结果来自预设查询序列。各策略在同一起点和同一调用预算下比较，代表性恢复轨迹见报告。

## 实验代码

| 文件 | 用途 |
|---|---|
| `experiment.py` | 模拟工具、八个案例、三种策略及统一执行流程 |
| `benchmark_recovery.py` | 四种执行前检查策略及可复现的配对实验 |
| `versioned_recovery.py` | 按错误类型校验历史证据版本 |
| `benchmark_versioned_recovery.py` | 动态目录与观测快照仿真、五组配对对照及边界场景 |
| `model_connection.py` | 模型接口适配、配置检查及冻结版本校验 |
| `run_real.py` | 真实模型实验入口 |
| `model_prompt.txt` | 两个模型组共用的提示词 |
| `model_config.example.json` | 无凭据配置示例 |
| `cases/cases.json` | 八个案例的配置 |
| `frozen/v0.1/` | 实验入口校验所依赖的冻结代码、说明及摘要 |
| `test_*.py` | 四十六项离线测试 |

## 运行

需要 Python 3.10 及以上，实验代码只使用标准库。

```sh
python3 -m unittest -v
python3 benchmark_versioned_recovery.py --output versioned-replay.json
cmp versioned-replay.json cases/versioned-recovery-benchmark.json
python3 benchmark_recovery.py --output recovery-benchmark.json
cmp recovery-benchmark.json cases/recovery-benchmark.json
```

上述命令运行离线测试，复跑补充实验并对比确定性 JSON 结果，无需网络或 API 密钥。

真实模型实验可先复制 `model_config.example.json` 为 `model_config.local.json`，填写接口和模型设置，并在本地环境变量 `RECOVERY_API_KEY` 中配置密钥。查看 `python3 run_real.py --help` 可获取运行参数。

## 交付文件

最终报告位于 `report/正式报告.pdf`，首页列明仓库和聊天记录的访问位置。聊天记录采用作者指定的 Markdown 文件，包含已导出的可见消息和粘贴附件。

真实模型运行日志和请求账本保存在本地。公开复现实验逐步记录候选动作、工具返回、拒绝判定及其历史证据。

报告封面已填写姜宇、学号 22634017、海洋学院及课程海洋数据挖掘，个人反思结合大模型自动驾驶研究方向展开。

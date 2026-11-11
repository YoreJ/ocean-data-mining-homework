# 工具调用失败后，有必要让 AI 决定下一步吗？

浙江大学课程实验作业。温度查询和故障由本地程序模拟，第三方 AI 负责选择下一步动作。比较固定规则、模型自主决策和模型加历史检查步骤。

- [项目仓库](https://github.com/YoreJ/-)
- [最终报告](report/正式报告.pdf)
- [本窗口聊天记录（2026-11-11）](conversations/本窗口聊天记录-2026-11-11.md)

## 实验代码

| 文件 | 用途 |
|---|---|
| `experiment.py` | 模拟工具、八个案例、三种策略及统一执行流程 |
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
python3 experiment.py --output results/local-program-check
```

上述命令执行离线程序检查。人工预写动作的检查结果应按程序测试解释。新运行请使用尚不存在的输出目录。

真实模型实验可先复制 `model_config.example.json` 为 `model_config.local.json`，填写接口和模型设置，并在本地环境变量 `RECOVERY_API_KEY` 中配置密钥。查看 `python3 run_real.py --help` 可获取运行参数。

## 交付文件

最终报告位于 `report/正式报告.pdf`，首页列明仓库和聊天记录的访问位置。聊天记录采用作者指定的 Markdown 文件，包含已导出的可见消息和粘贴附件。

运行日志、请求账本、审计记录、旧版对话导出、模板、报告源文件和排版过程文件保存在本地。上传范围由 `.gitignore` 中的文件清单限定。

报告封面中的姓名、学号、院所、课程、授课教师及个人反思仍待作者补充。

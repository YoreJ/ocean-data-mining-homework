# 工具调用失败后，有必要让 AI 决定下一步吗？

浙江大学课程报告与可核查实验项目。

公开仓库 https://github.com/YoreJ/-

## 先读什么

- 正式报告 `report/正式报告.pdf`
- 可编辑正文 `report/report.md`
- 按本地浙江大学模板排版的可编辑源文件 `report/正式报告.tex`
- 实验结果 `results/claude-pilot-20261109/experiment/summary.json`
- 结果审计 `results/claude-pilot-20261109/result-audit.json` 和 `audit.json`
- 两个窗口的实际聊天记录 `conversations/`，粘贴附件在其 `attachments/` 子目录

封面姓名、学号、院所、课程、授课教师，以及个人反思仍待作者补充。报告采用最后确认的正文，仅将仓库占位更新为实际地址并按模板排版。

## 这项实验做了什么

温度、站点和故障均为本地模拟。真实第三方 AI 只选择下一步动作。比较固定规则、模型自主决策和模型加历史检查步骤。每次恢复最多四次工具调用、六次模型调用。三组八例的结果类别逐案相同。正式模型试跑有三十四次请求，独立连通性检查另有一次。检查步骤运行十七次，其中十四个查询提案适用两条约束，拒绝次数为零。具体限制、用量疑问与费用未知均在报告中说明。

## 环境与离线检查

实验代码只使用 Python 标准库，建议 Python 3.10 及以上。无需安装第三方 Python 依赖。

```sh
python3 -m unittest -v
python3 experiment.py --output results/local-program-check
```

这两条命令不调用真实 AI。输出中的预写动作序列属于程序测试，不能当作模型实验。请为新结果选择尚不存在的目录，避免覆盖历史记录。

八个冻结案例见 `cases/cases.json` 与 `experiment.py`。快照和行为冻结摘要在 `frozen/v0.1/`，行为修改原因须记入 `CHANGES.md`。本次交付未增加模型调用，也未修改冻结策略。

## 结果与证据目录

| 目录或文件 | 用途 |
|---|---|
| `results/api-preparation-program-check/rule_baseline/` | 报告采用的八例固定规则记录 |
| `results/claude-pilot-20261109/experiment/runs/` | 十六次正式模型任务轨迹 |
| `results/claude-pilot-20261109/experiment/request_ledgers/` | 实际请求、原始响应和用量 |
| `results/claude-pilot-20261109/connectivity/` | 独立连通性检查，排除于正式结果 |
| `results/claude-pilot-20261109/experiment/manifest.json` | 实际顺序、请求型号与生成设置 |
| `results/claude-pilot-20261109/audit.json` | 检查点、提示词与用量一致性审计 |
| `results/claude-pilot-20261109/result-audit.json` | 动作分类、检查耗时和判定审计 |
| `results/claude-pilot-20261109/reference-check.json` | 已有一手引用核验记录 |
| 其余 `results/` 子目录 | 较早接口准备、失败探测或离线程序测试，不能混入正式结果 |

请求日志包含构造的任务信息，不包含鉴权请求头或 API 密钥。网关返回的 token 数保留原始口径，底层模型身份与缓存计量尚不能独立核实，金额未知。

## 模型接口与凭据

实际试跑通过第三方 API，请求及响应标识均为 `claude-sonnet-4-6`。历史记录保留真实提供商信息，不将其当作底层身份认证。客户端自动重试关闭。

仓库中的配置只提供无凭据示例。若将来需要新增实验，在本地复制并填写配置，密钥仅通过环境变量提供。

```sh
cp model_config.example.json model_config.local.json
# 填写提供商、HTTPS 接口地址、协议、准确模型标识及生成设置。
# 在自己的本地环境中设置 RECOVERY_API_KEY，切勿写入仓库。
python3 model_connection.py --config model_config.local.json
```

上述检查命令不会发起模型请求。真实请求入口为 `run_real.py`，运行前须先查看其 `--help` 和实验预算。本次交付不要求重新运行付费实验。

## 编辑与生成报告

原始模板保存在 `浙江大学课程论文模板/`。正式版本沿用其 A4、2.5 厘米页边距、12 磅正文、封面、目录和参考文献组织。中文采用宋体 SC，西文采用 Times New Roman。模板已有 CC BY 4.0 署名保留，改动说明见 `report/排版与交付说明.md`。

需要 XeLaTeX、BibTeX 及相应字体。当前文件在 macOS 和 TeX Live 2026 下生成。其他系统需安装上述字体或明确替换字体设置。源模板本身为 LaTeX，可编辑交付采用 LaTeX 和 Markdown。Word 转换的中文导出异常版本未作为正式文件交付。

```sh
python3 tools/build_report.py
cd report
xelatex -interaction=nonstopmode -halt-on-error 正式报告.tex
bibtex 正式报告
xelatex -interaction=nonstopmode -halt-on-error 正式报告.tex
xelatex -interaction=nonstopmode -halt-on-error 正式报告.tex
```

修改正文应先编辑 `report/report.md`，再生成排版源文件。封面资料在 `tools/build_report.py` 中更新。流程图的编辑文本为 `report/figures/recovery-flow.mmd`，已排版图片为同目录 `recovery-flow.png`。

## 对话与公开安全

`conversations/README.md` 说明实际记录的导出范围、时点及路径脱敏。完整实验请求记录另存在 `results/`。系统提示、内部推理、工具载荷和本地原始会话数据库不属于上传的聊天正文。

`.gitignore` 排除本地模型配置、凭据、缓存、排版临时文件和私有构建目录。公开前检查记录在 `PUBLICATION_CHECK.md`。公开上传不包含原始本机配置或登录文件。

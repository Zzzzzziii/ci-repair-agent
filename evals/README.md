# 真实 CI 故障评测

这个评测回答两个问题。看到一段 pytest 报错后，检索器能找到出错的函数吗？Agent 交出的补丁通过公开测试后，能处理另一组输入吗？

## 数据与判定

20 个案例取自三个上游仓库的真实修复提交，分别来自 [more-itertools](https://github.com/more-itertools/more-itertools)（7 个）、[boltons](https://github.com/mahmoud/boltons)（7 个）和 [humanize](https://github.com/python-humanize/humanize)（6 个）。每条清单记录固定的修复提交、父提交、上游链接、包路径、根因函数和三项检查。选择范围是能用 CPython 3.12、pytest 和包内源码独立复现的逻辑错误。需要外部服务、额外依赖或无法构造独立断言的故障不纳入本版。

| 检查 | 给 Agent 看 | 坏版本预期 | 上游修复预期 |
| --- | --- | --- | --- |
| 公开目标测试 | 是 | 失败 | 通过 |
| 公开回归测试 | 是 | 通过 | 通过 |
| 隐藏测试，改变输入或边界条件 | 独立评测时使用 | 失败 | 通过 |

5 个较早修复放在 [开发集](cases/development.jsonl)，2026 年 6 月 1 日以后修复的 15 个案例放在[固定评测集](cases/evaluation.jsonl)。这些测试是在本项目中按故障行为编写的最小复现。评测覆盖表中的三个检查，上游库完整测试套件的覆盖面更大。

评测器从上游镜像导出故障版本的包源码和许可证，用公开测试创建单提交、无远端的 Git 仓库。Agent 的文件工具、Git 工具和检索索引都指向这个仓库。模型生成的候选代码在无网络的 Docker 容器里执行，只挂载当前源码、pytest 记录器和本次测试的输出目录。公开验证通过后，外部评测器在另一份干净副本应用同一补丁，加入隐藏测试，再独立执行。上游修复只用于校准。本协议控制运行时可见性，公开项目在模型训练数据中的暴露情况未知。

## 已实测的结果

本机 Windows，CPython 3.12.14，pytest 9.2.0。定位查询使用公开失败的节点名和断言日志。每个案例标注一个根因符号，以“文件路径 + 符号”精确匹配，最多返回 6 段，上下文预算 7,000 个估算 token。

同一组 20 个案例也在 Linux Docker 容器内完成校准，两种环境得到相同的复现数和定位命中数。另用 `case-04` 的上游补丁走了一次完整 CLI 验收，记录器在容器内跑通了失败复现和补丁回归。

| 数据集 | 故障复现并通过参考修复验证 | 字面检索 Recall@6 | BM25 加符号及导入线索 Recall@6 |
| --- | --- | --- | --- |
| 开发集 | 5/5 | 2/5 | 4/5 |
| 固定评测集 | 15/15 | 7/15（46.7%） | 11/15（73.3%） |

例如 `case-06` 的公开日志只报告“预期抛出 ValueError”，没有指出实现中的出错行。BM25 的前六段未命中 `sliced`。同一配置也漏掉了 `case-10`、`case-11`、`case-14`。这次结果支持保留 BM25 作为默认定位方式。DeepSeek 的独立解决率、回归反馈增益、记忆增益和模型用量有待真实模型运行测量。

## 本地复跑

在项目根目录运行以下 PowerShell 命令。首次运行需要 GitHub 网络连接以下载三个上游镜像。再次运行会复用镜像。每次用新的输出目录保护之前的实验数据。

```powershell
uv sync --frozen --python 3.12
.\.venv\Scripts\python.exe evals/run.py real --jobs 3 --output .ci-repair/real-01
```

打开 `.ci-repair/real-01/results.json` 查看分组汇总与逐案例记录。打开 `case-04/calibration.json` 查看原始故障、上游修复结果、检索到的函数及耗时。每组的 `evidence/junit.xml` 保存 pytest 原始判定。用 `--split evaluation` 只跑固定集，用 `--case case-04` 只跑一个故障，用 `--cache` 指定上游镜像目录。已有的合成案例仍使用 `python evals/run.py` 的原入口。

有 Docker 和模型凭据后，可以先对一个案例运行四组实验。在当前进程环境配置 `DEEPSEEK_API_KEY`，并将 `CI_REPAIR_MODEL` 设为账号使用的完整 `deepseek:模型名`。

```powershell
Get-Content -Raw evals/Dockerfile | docker build -t ci-repair-eval -
.\.venv\Scripts\python.exe evals/run.py real --case case-04 --image ci-repair-eval --model $env:CI_REPAIR_MODEL --output .ci-repair/model-01
```

| 模式 | 模型可用的信息 |
| --- | --- |
| `tools` | 公开报错、源码读取、字面搜索和 Git diff |
| `rag` | 基础工具、自动检索上下文、符号及索引查询 |
| `feedback` | 以上信息加上上一版补丁失败的断言和回归结果 |
| `memory` | 以上信息加上同仓库较早的开发任务留下的经验 |

四组都走同一顺序修复循环。每个目标任务上限是 12 次模型调用、18 次工具调用、3 个候选、16 次测试调用和 900 秒。用 `--modes tools rag` 选择子集，`--repeat 3` 可记录同一配置的波动，每次从新的任务目录开始。记忆组先单独运行同仓库时间最近且早于目标的开发案例，种子任务另计预算与耗时，经验来自 Agent 的实际尝试。每个目标、实验组和重复轮次有独立存储，固定集各案例之间不传递经验。

## 在 GitHub 上运行

1. 将工作流合并到默认分支，打开 **Actions → Real bug benchmark → Run workflow**。
2. 保留 `calibrate` 和 `case-04` 可在 GitHub 的 Linux 环境验证一个案例并生成报告，无需密钥。
3. 选 `compare` 会使用仓库 Secret `API_KEY` 和 Variable `CI_REPAIR_MODEL`，在所选案例上调用 DeepSeek 跑四组。选 `all` 会对全部 15 个评测案例运行，模型用量会随案例、实验组和重复次数增加。
4. 下载 `benchmark-summary` 看总表，下载对应案例的 artifact 看分项结果、任务报告、模型与工具调用、候选 diff 及测试记录。

## 读懂统计

- **独立解决率**分别以全部尝试和通过校准的尝试为分母。环境故障、模型停止和预算耗尽保留原因。多次重复按尝试计数。
- **误接受率**的分母是 Agent 已接受且独立裁判得到确定结论的补丁，分子是其中在独立检查中失败的补丁。裁判自身出错单列；分母为零时比率为 `null`。
- **回归拦截率**统计触发回归违规的候选数占已验证候选数的比例。不同的正确实现也可以通过，裁判按行为判定。
- **成本**分别记录实际模型调用、工具调用、测试调用、供应商返回的 token、索引和查询时间、独立裁判时间、记忆种子成本。模型未返回的 token 数单列。每个目标的 `report` 指向调用轨迹与补丁。运行报告同时记录上游提交、源码指纹、环境、请求的模型名、供应商返回的模型名（如有）及预算。

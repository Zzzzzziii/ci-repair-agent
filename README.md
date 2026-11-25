# CI 单测失败修复 Agent

把一个 Git 仓库和 pytest CI 日志交给 CLI。Agent 在指定提交上复现失败，调用工具读取和搜索源码，生成候选 diff，再运行原用例和回归测试。报告列出每个候选的验收证据。通过时另存可审查的 `fix.diff`。

## 安装和使用

需要 Git、Python 3.12 或以上版本和 pytest。使用 uv 安装本项目：

```powershell
uv sync
uv run ci-repair --help
```

在本地环境中设置 `DEEPSEEK_API_KEY`，将 `CI_REPAIR_MODEL` 设为 `deepseek:deepseek-flash`。LangChain 负责绑定读取、搜索和 Git diff 工具。目标仓库的依赖应提前装好，必要时通过 `--python` 指定其虚拟环境。

```powershell
uv run ci-repair fix --repo D:\checkout-service --log .\ci-error.log --commit a1b2c3d
uv run ci-repair fix --repo D:\checkout-service --test 'tests/test_checkout.py::test_coupon' --python 'D:\checkout-service\.venv\Scripts\python.exe'
```

- `--log -` 从标准输入读取日志。识别 pytest 的 `FAILED path::test_name` 摘要，也接受 GitHub Actions 单时间戳前缀。日志缺少准确用例时传 `--test`，可重复。
- `--commit` 指定失败提交。缺省时读取明确的 `GITHUB_SHA=<SHA>` 或 `commit: <SHA>` 日志行，最后回退到本地 `HEAD`。报告记录版本来源。
- `--cwd` 指定相对仓库的测试根目录。测试选择器也相对于这个目录。默认回归范围是该目录按项目配置发现的测试套件，可用可重复的 `--regression` 缩小范围。
- `--strategy batch` 一次生成最多两个候选，默认启用。`--strategy sequential` 先验证一个候选，失败后把测试反馈交给模型再试一次。
- `--patch` 可重复传入一或两份现成 diff，走相同验收流程，无需模型密钥。

### 接入自己的项目

将[配置示例](examples/ci-repair.example.toml)复制到目标 Git 仓库根目录，命名为 `.ci-repair.toml`，按项目布局修改。`cwd` 和 `source_paths` 相对于仓库根目录，`regression` 相对于 `cwd`。配置中的 Python 相对路径以仓库根目录为准，也可以写 PATH 里的解释器命令。`fix` 和 `doctor` 自动读取该文件，也接受 `--config` 指定其他文件。命令行传入的值优先于配置和默认值。

```powershell
uv run ci-repair doctor --repo D:\my-service --import my_package
```

`doctor` 输出 Python 和 pytest 版本、工作目录、回归范围，以及 `--import` 指定模块在当前检出中的解析路径。配置 `source_paths = ["src"]` 后，`fix` 会在每个临时 worktree 中把该版本的 `src` 加到 `PYTHONPATH` 前面。实际使用的配置会保存在任务清单里，暂停后继续沿用。

### 修复报告

输出中的任务 ID 对应 `~/.ci-repair/runs/<task_id>/report.json`。要换位置，在子命令前传 `--state-dir`。验收保留基线中已通过的用例，并单独记录基线已有的其他失败。首版支持修改已有的 Python 业务源码。补丁应用在临时 Git worktree 中，用户的当前工作目录不受影响。测试以目标解释器在本机执行，请用于自己的仓库。

Agent 开始执行修复任务前，会在任务目录生成 `manifest.json`，记录任务 ID、代码提交及其来源、失败用例和所选模型。传入 `--log` 时，输入日志保存在 `artifacts/input/ci.log`。只有 `--test` 时，清单里的 `input_log` 为 `null`。恢复暂停的任务时，原来的清单和日志会保留下来。可以从 `report.json` 的 `manifest` 字段找到清单。

使用模型时，可以从 `report.json` 的 `model_calls` 找到 `artifacts/model/` 下的记录。`purpose` 区分本地初始化、探索代码和生成补丁，`status` 区分成功、初始化失败、请求失败和补丁格式无效。每条记录包含开始时间、耗时、模型返回的 Token 用量及异常类型。进程在模型步骤中被终止时，可以在任务目录下找到状态为 `running` 的记录。

模型请求读取、搜索或查看 Git diff 时，`report.json` 的 `tool_events` 指向 `artifacts/tools/` 下的记录。每条记录包含请求参数、执行状态、耗时和工具返回给模型的文本，`model_call_id` 对应触发它的模型记录。工具本身的行数和输出长度限制仍然生效。匹配已配置模型 API 密钥的文本会被遮盖，结果中可能包含仓库源码。

`report.json` 的 `events` 指向同一任务目录里的 `events.jsonl`。按行查看复现失败、生成补丁、候选验收和收尾的开始与结果。验收事件记录候选编号，`step_id` 将同一次节点执行的开始和结束关联起来。暂停和恢复会继续写入同一份时间线，恢复进度由 SQLite 检查点决定。

每个候选补丁单独保存在任务目录的 `artifacts/candidates/candidate-N.diff`。报告里可以从 `candidates` 的 `diff_artifact` 或对应 `validations` 的 `candidate_diff` 打开它。任务暂停时也能查看已记录的候选。通过验收后，`fix.diff` 保存从验收工作区导出的最终改动。

每次 pytest 执行的完整终端输出保存在任务目录的 `artifacts/pytest/<run_id>/stdout.log` 与 `stderr.log`。pytest 写出逐用例报告时，同目录还有 `pytest.json`，其中包含用例各阶段的状态、耗时和失败详情。在 `report.json` 中，从 `reference.target`、`reference.regression` 或某个 `validations` 条目的测试结果读取 `artifacts`，就能找到对应文件。路径相对于 `report.json` 所在目录。测试超时后也可查看已经写出的日志。

每次测试结果还记录实际执行的 `command` 和 `cwd`。达到 `timeout` 时，执行器会终止当前测试的进程树，报告为 `timeout`，并保留停止前的日志和输出摘要。系统无法完成进程树清理时，`cleanup_error` 会说明问题。

### 暂停和记忆

加 `--pause-after-candidate` 可在第一个候选验证后主动暂停。也可以在进程退出后查看已保存进度并恢复：

```powershell
uv run ci-repair status <task_id>
uv run ci-repair resume <task_id>
```

LangGraph 把任务状态存进 SQLite。完成验证的候选会被记住，恢复后从待执行的节点继续。验收通过的修复经验可以在新任务中按同一仓库、用例名和失败特征检索，帮助模型定位。使用 `--no-memory` 可关闭本次任务的历史检索和写入。

### 验证和评测

```powershell
uv run pytest -q
uv run python evals/run.py --output .ci-repair/eval-offline --limit 5
uv run python evals/run.py --output .ci-repair/eval-live --model $env:CI_REPAIR_MODEL --compare-memory
```

自动化测试覆盖逐用例结果、工具调用、补丁验收、两份独立仓库和进程退出后恢复。评测脚本准备 20 个合成案例，覆盖 5 类缺陷。离线模式使用已知补丁检查评测流程。模型模式比较批量候选、顺序重试和历史记忆，并用模型未见过的测试验收输出。结果写入 `results.json` 和 `summary.json`。

GitHub Actions 的 [CI Repair Agent baseline](.github/workflows/ci-repair-demo.yml) 会在结算示例代码推送时运行结算用例。要建立两组模型基线，在 GitHub 的 **Actions → CI Repair Agent baseline → Run workflow** 选择 `main` 并运行。运费 `shipping` 和结算 `checkout` 各有一个任务，分别采集 pytest 失败日志、调用同一个 Agent CLI 并验收候选补丁。

工作流从仓库 Secret `API_KEY` 读取 DeepSeek 密钥，默认使用 `deepseek:deepseek-flash`。仓库变量 `CI_REPAIR_MODEL` 可以指定另一个支持工具调用的 DeepSeek 模型。每个任务结束后都会尝试上传自己的记录。在运行详情的 **Artifacts** 下载 `repair-results-shipping` 或 `repair-results-checkout`，从 `report.json` 查看验收结果，再沿 `model_calls`、`tool_events` 和候选补丁路径查看证据。

各阶段的设计原因和验收目标见 [开发计划](DEVELOPMENT_PLAN.md)。

真实仓库接入、代码 RAG、反馈修复和记忆的后续开发，按 [进阶实施计划与实现讲解](IMPLEMENTATION_ROADMAP.md) 推进。其中包含 2026 年 11 月 18 日的代码审查结果、已复现的环境问题和逐阶段完成条件。

实际编码时使用 [分阶段文件与实现清单](STAGE_IMPLEMENTATION_PLAN.md)，逐项完成对应文件、接口与验收案例。

## 最小运费案例

示例规则是普通地区满 200 元免运费，偏远地区固定收取 25 元。`examples/shipping/shipping.py` 故意将普通地区的条件写成 `> 200`，供后续修复流程使用。

在项目根目录用 PowerShell 创建环境：

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.uv-python'
uv python install 3.12 --no-bin
uv sync --managed-python
```

运行整个示例测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q examples/shipping
```

预期有 1 条失败、3 条通过。失败用例是 `test_standard_shipping_at_threshold_is_free`，实收 12 元，预期 0 元。其他用例覆盖门槛上下两侧以及偏远地区。

只复跑失败用例：

```powershell
.\.venv\Scripts\python.exe -m pytest -q examples/shipping/test_shipping.py::test_standard_shipping_at_threshold_is_free
```

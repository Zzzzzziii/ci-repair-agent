# CI 单测失败修复 Agent

把一个 Git 仓库和 pytest CI 日志交给 CLI。Agent 在指定提交上复现失败，调用工具读取和搜索源码，生成候选 diff，再运行原用例和回归测试。报告列出每个候选的验收证据。通过时另存可审查的 `fix.diff`。

项目自带 20 个上游真实故障的最小复现，可独立验证补丁、比较代码检索和修复策略。数据来源、实测结果和复跑命令见[真实故障评测说明](evals/README.md)。

要给自己的仓库接入修复流程，请从[本地与 GitHub Actions 使用指南](docs/usage.md)开始。Action 在失败提交上运行，生成 `report.md`、结构化证据和通过验收的补丁。可以用[三段演示](docs/demo.md)追踪跨仓库定位、回归失败和跨 runner 恢复。

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
- `--strategy batch` 一次生成最多两个候选，默认启用。`--strategy sequential` 逐个验证候选。模型每轮可以调用一个只读工具、提交完整补丁或说明停止原因。
- `--patch` 可重复传入一或两份现成 diff，走相同验收流程，无需模型密钥。
- `--seed-patch` 用一份指定的 diff 作为顺序修复的首个候选。首个候选被拒后，模型可依据反馈提出第二份补丁。此模式需要配置模型。

### 复现一次回归后重试

在 CI Repair Agent 仓库根目录运行以下命令。预置补丁会让普通地区的免运费测试通过，同时把偏远地区运费错误地降为 0 元。Agent 会把失败断言、堆栈和候选改动交给已配置的模型，新候选要重新通过目标与回归测试。

```powershell
uv run ci-repair --state-dir .ci-repair/feedback-demo fix --repo . --test 'examples/shipping/test_shipping.py::test_standard_shipping_at_threshold_is_free' --regression examples/shipping --seed-patch evals/fixtures/shipping-regression.diff
```

`report.json` 的 `retry_feedback` 指向实际提交给模型的有界反馈，`validations` 保留每份候选的测试结果。反馈中的测试产物路径可用于查看原始断言和日志。

顺序模式先验证原失败用例，再检查同一测试文件里基线通过的相关用例，最后运行声明的回归范围。每步都从同一提交重建候选工作区，核对实际应用的 diff。第一个补丁引入的偏远地区反例会出现在反馈中，模型可以读源码重新判断、直接提交修订，或选择结束。重复 diff 会收到纠正提示，已验证的重复候选会跳过测试。

默认预算为 12 次模型请求、18 次工具调用、3 个不同候选、16 次 pytest 执行和 900 秒。命令行可分别传 `--max-model-calls`、`--max-tool-calls`、`--max-candidates`、`--max-tests` 和 `--max-seconds`。pytest 次数包括基线，截止时间包括暂停。调用前先持久化预留额度，中断后继续沿用。已返回的模型用量写进报告，未返回的用量记为 null。模型主动结束、格式连续无效和预算用完分别由 `model_stopped`、`invalid_decision`、`budget_exhausted` 标识。

在报告里按候选的 `parent_id`、`model_call_id` 和验收结果的 `candidate_id` 可以追溯修订过程。`events.jsonl` 中的 `decision` 记录模型每次选择，`duplicate_candidate` 记录重复补丁。要用仓库的 DeepSeek 凭据验证此示例，在 GitHub Actions 手动运行 **CI Repair Agent baseline**，勾选 **Run the shipping regression feedback loop**。运行结束后下载 **repair-results-shipping** 查看实际测试和模型记录。

### 接入自己的项目

将[配置示例](examples/ci-repair.example.toml)复制到目标 Git 仓库根目录，命名为 `.ci-repair.toml`，按项目布局修改。`cwd` 和 `source_paths` 相对于仓库根目录，`regression` 相对于 `cwd`。配置中的 Python 相对路径以仓库根目录为准，也可以写 PATH 里的解释器命令。`fix` 和 `doctor` 自动读取该文件，也接受 `--config` 指定其他文件。命令行传入的值优先于配置和默认值。

```powershell
uv run ci-repair doctor --repo D:\my-service --import my_package
```

`doctor` 输出 Python 和 pytest 版本、工作目录、回归范围，以及 `--import` 指定模块在当前检出中的解析路径。配置 `source_paths = ["src"]` 后，`fix` 会在每个临时 worktree 中把该版本的 `src` 加到 `PYTHONPATH` 前面。实际使用的配置会保存在任务清单里，暂停后继续沿用。

要验证测试加载的源码，在配置中加 `import_modules = ["my_package"]`，或给 `fix` 传 `--import my_package`。`doctor` 也会检查配置里的模块能解析到哪里。运行 pytest 时，报告的 `imports` 字段记录指定模块实际加载过的路径，`runtime` 字段记录测试进程的 Python 和 pytest 版本。目标测试或回归测试发现模块来自 worktree 外部时，修复任务或候选会被标为 `blocked`。每个指定模块至少要在一次目标测试或回归测试的 pytest 进程中被观察到。配置项应填写本项目的模块，混合命名空间包可填写项目所属的具体子模块。

配置 `pytest_args = ["-m", "unit"]` 可以声明额外 pytest 参数，列表的每项是一个命令行参数。重复传 `--pytest-arg=-m --pytest-arg=unit` 可覆盖配置。基线和候选沿用同一组参数，`doctor` 和 `manifest.json` 会列出它们。

测试结果的 `runtime.environment_fingerprint` 汇总实际解释器、pytest 与安装包版本、pytest 参数、相关环境选项和配置文件内容的摘要。首次基线目标测试的指纹也写入清单。候选与基线的同类测试指纹不同会得到 `environment_changed`，例如暂停后改变了 pytest 插件加载设置。

### 修复报告

输出中的任务 ID 对应 `~/.ci-repair/runs/<task_id>/report.json`。要换位置，在子命令前传 `--state-dir`。验收保留基线中已通过的用例，并单独记录基线已有的其他失败。首版支持修改已有的 Python 业务源码。补丁应用在临时 Git worktree 中，用户的当前工作目录不受影响。测试以目标解释器在本机执行，请用于自己的仓库。

Agent 开始执行修复任务前，会在任务目录生成 `manifest.json`，记录任务 ID、代码提交及其来源、失败用例和所选模型。传入 `--log` 时，输入日志保存在 `artifacts/input/ci.log`。只有 `--test` 时，清单里的 `input_log` 为 `null`。恢复暂停的任务时，原来的清单和日志会保留下来。可以从 `report.json` 的 `manifest` 字段找到清单。

使用模型时，可以从 `report.json` 的 `model_calls` 找到 `artifacts/model/` 下的记录。`purpose` 区分本地初始化、探索代码和生成补丁，`status` 区分成功、初始化失败、请求失败和补丁格式无效。每条记录包含开始时间、耗时、模型返回的 Token 用量及异常类型。进程在模型步骤中被终止时，可以在任务目录下找到状态为 `running` 的记录。

模型请求读取、搜索或查看 Git diff 时，`report.json` 的 `tool_events` 指向 `artifacts/tools/` 下的记录。每条记录包含请求参数、执行状态、耗时和工具返回给模型的文本，`model_call_id` 对应触发它的模型记录。工具本身的行数和输出长度限制仍然生效。匹配已配置模型 API 密钥的文本会被遮盖，结果中可能包含仓库源码。

`report.json` 的 `events` 指向同一任务目录里的 `events.jsonl`。按行查看复现失败、生成补丁、候选验收和收尾的开始与结果。验收事件记录候选编号，`step_id` 将同一次节点执行的开始和结束关联起来。暂停和恢复会继续写入同一份时间线，恢复进度由 SQLite 检查点决定。

每个候选补丁单独保存在任务目录的 `artifacts/candidates/candidate-N.diff`。报告里可以从 `candidates` 的 `diff_artifact` 或对应 `validations` 的 `candidate_diff` 打开它。任务暂停时也能查看已记录的候选。通过验收后，`fix.diff` 保存从验收工作区导出的最终改动。

每次 pytest 执行的完整终端输出保存在任务目录的 `artifacts/pytest/<run_id>/stdout.log` 与 `stderr.log`。pytest 写出逐用例报告时，同目录还有 `pytest.json`，其中包含用例各阶段的状态、耗时和失败详情。在 `report.json` 中，从 `reference.target`、`reference.regression` 或某个 `validations` 条目的测试结果读取 `artifacts`，就能找到对应文件。路径相对于 `report.json` 所在目录。测试超时后也可查看已经写出的日志。

每次测试结果还记录实际执行的 `command` 和 `cwd`。达到 `timeout` 时，执行器会终止当前测试的进程树，报告为 `timeout`，并保留停止前的日志和输出摘要。系统无法完成进程树清理时，`cleanup_error` 会说明问题。

`collected` 列出 pytest 筛选后选中的用例，`session_finished` 表示会话结束的证据已写入。执行只完成了收集时，已选中却没有运行的用例会标为 `not_run`，即使 pytest 返回 0，Agent 也会报告 `incomplete`。验收会比较各候选与基线的收集范围，`missing_tests` 记录消失的用例，`unexecuted_tests` 记录已收集但没运行的用例。测试在收集后超时时，可以从 `pytest.json` 查看已保存的收集清单。

`failure_kind` 区分业务测试失败、缺少依赖、收集错误、fixture 错误、会话中断和超时。遇到 `ModuleNotFoundError` 时，`missing_dependencies` 会记录缺少的导入名及发生阶段。测试执行无法提供可靠结果时，验收报告会标为 `blocked`，并给出 `reason_code`。基线没有复现失败时记录 `failure_not_reproduced`，可以通过 `target_outcomes` 查看用例实际是通过、跳过还是缺失。

`worktrees` 指向本次任务创建的临时工作区记录。每条记录包含用途、路径、检出的提交和清理状态。创建或清理失败、上次运行中断时留下的未确认记录会列在 `worktree_issues`，可和已采集的测试结果一起查看。

### 暂停和记忆

加 `--pause-after-candidate` 可在第一个候选验证后主动暂停。也可以在进程退出后查看已保存进度并恢复：

```powershell
uv run ci-repair status <task_id>
uv run ci-repair resume <task_id>
```

LangGraph 把任务状态存进 SQLite。完成验证的候选会被记住，恢复后从待执行的节点继续。验收通过的修复经验可以在新任务中按同一仓库、用例名和失败特征检索，帮助模型定位。使用 `--no-memory` 可关闭本次任务的历史检索和写入。

**恢复同一个任务。** 在原来的 `--state-dir` 下运行 `status` 能看到任务状态、提交、检查点、已验收候选数和下一个节点。进程中断后，`resume` 核对仓库身份、提交、候选 diff 和已保存的测试证据，并用目标解释器核对环境指纹。校验通过后，已提交到检查点的步骤直接复用，执行中的步骤在新的工作区重跑。外部模型请求结果未知时，已预留的额度继续计入预算。每次恢复尝试的结果可从报告的 `recovery` 打开。

```powershell
uv run ci-repair --state-dir .ci-repair status TASK_ID
uv run ci-repair --state-dir .ci-repair resume TASK_ID --repo D:\moved-service
```

目录搬迁后需要保留原状态目录、失败提交以及可复现的 Python 环境。仓库可以在项目 TOML 中设置稳定的 `repo_id = "my-team/service"`。默认使用去除身份凭据后规范化的远端地址生成标识。纯本地仓库把随机标识持久化在 Git 公共目录的 `ci-repair-id.sqlite`。需要指定移动后的外部配置时，可给 `resume` 传 `--config`。检查点早于仓库身份功能的任务沿用原位置恢复。

**在新任务中使用历史经验。** 每条经验记录来源任务、候选号、提交、假设和实际验收结果。被回归测试拒绝的候选也会保存，供模型参考已经观察到的失败。检索只返回当前仓库中启用、来源报告和验收产物可核查的记录。旧经验库首次访问时迁移。可以查看经验，按 ID 禁用或删除。

```powershell
uv run ci-repair --state-dir .ci-repair memory list --repo .
uv run ci-repair --state-dir .ci-repair memory show EXPERIENCE_ID --repo .
uv run ci-repair --state-dir .ci-repair memory disable EXPERIENCE_ID --repo .
uv run ci-repair --state-dir .ci-repair memory delete EXPERIENCE_ID --repo .
```

运行 `uv run python evals/memory.py --output .ci-repair/memory-comparison` 可复现三组离线对照，输出目录需为新目录。关闭记忆时使用已知正确的参考补丁，另外两组直接重放检索到的历史补丁。三组的当前失败断言相同，旧规则“所有订单免运费”能通过当前的目标用例，但会破坏低于门槛仍需收费的回归用例。各组测试结果和来源任务保存在 `results.json`。

### 从失败用例检索源码

模型修复默认启用 `--retrieval bm25`。Agent 从复现时的断言和测试节点提取查询，使用 AST 把失败提交中跟踪的 Python 文件切成函数、方法、类和模块代码块。SQLite FTS5 负责 BM25 排序，Agent 同时匹配完整符号名，并沿可解析的静态导入最多追两跳。相同的 Git blob 可以跨提交复用解析结果。再次修复同一个提交时，Agent 可以直接重用索引。

模型先收到在上下文预算内选出的源码，再通过 function calling 选择 `list_files`、`search_symbols`、`retrieve_code`、`read_file`、`search_code` 或 `git_diff`。例如结算测试只显示 `checkout_total(200, 30)` 算错，检索会找到调用方和它引用的 `delivery.shipping_fee`，模型可以打开具体行验证免运费门槛。补丁仍要通过目标与回归测试。源码索引与只读工具都使用失败提交，当前目录里的未提交修改不会出现在证据中。

```powershell
uv run ci-repair --state-dir .ci-repair/checkout-rag fix --repo . --test 'examples/checkout/test_checkout.py::test_discount_keeps_free_shipping' --regression examples/checkout --no-memory
```

在对应任务的 `report.json` 中打开 `retrieval` 引用的 JSON。每条记录包含查询、提交、命中片段、召回来源、实际放进上下文的片段、估算的上下文大小和索引用时。上下文预算用 UTF-8 字节数近似约束。候选的 `evidence` 会校验文件及行号，超出源码范围的引用放入 `unverified_evidence`。静态导入只提供调用线索，动态导入与运行时分派仍需通过源码和测试核实。索引处理不超过 512 KiB 的 Python 文件。

`--retrieval literal` 可用作对照，只按字面搜索首个调用名或标识符。显式传 `--retrieval hybrid --embedding-model provider:model` 可开启向量检索，使用余弦相似度召回和 RRF 排名融合。模型 ID 也可通过 `CI_REPAIR_EMBEDDING_MODEL` 或项目配置中的 `embedding_model` 设置。选用远程向量服务时，查询和尚未缓存的源码片段会发送给该服务。默认 BM25 和 `--patch` 模式均无需向量服务。

### 验证和评测

```powershell
uv run pytest -q
uv run python evals/run.py --output .ci-repair/eval-offline --limit 5
uv run python evals/run.py --output .ci-repair/eval-live --model $env:CI_REPAIR_MODEL --compare-memory
```

自动化测试覆盖逐用例结果、工具调用、补丁验收、两份独立仓库和进程退出后恢复。评测脚本准备 20 个合成案例，覆盖 5 类缺陷。离线模式使用已知补丁检查评测流程。模型模式比较批量候选、顺序重试和历史记忆，并用模型未见过的测试验收输出。结果写入 `results.json` 和 `summary.json`。

下面的评测从指定提交重新运行运费和结算的失败测试，再按标注的文件及函数计算进入上下文的 Recall@6。结果写入指定目录的 `results.json`，同时保存检索耗时和上下文大小。加 `--embedding-model provider:model` 可测混合检索，加 `--repair-model provider:model` 会用同一个生成模型比较各检索模式的补丁验收结果。两种参数分别需要各自的服务凭据。

```powershell
uv run python evals/retrieval.py --output .ci-repair/retrieval-eval --top-k 6
```

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

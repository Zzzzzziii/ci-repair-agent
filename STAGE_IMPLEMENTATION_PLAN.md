# CI 修复 Agent 分阶段文件与实现清单

更新于 2026 年 11 月 18 日。

本清单承接 [进阶实施计划](IMPLEMENTATION_ROADMAP.md)，把 0 至 6 阶段落实到文件、接口、调用关系与验收案例。文件名为计划路径，表中标记“新增”的文件在对应阶段创建。现有 CLI、示例和集成测试是后续开发的起点。

首个发布目标是接入至少三个配置明确的真实 Python + pytest 仓库。核心交付为代码检索、测试反馈修复、可靠执行与任务恢复，配套真实模型评测与 GitHub Actions 演示。Java、Node.js 等适配可以在取得对应案例后沿测试适配边界扩展。

## 全部阶段与开发顺序

| 阶段 | 完成后的能力 | 先做的文件 | 后续依赖 |
| --- | --- | --- | --- |
| 0 | 每次真实模型运行都有完整证据 | `schemas.py`、`artifacts.py`、`model.py` | 为后续阶段提供运行基线 |
| 1 | 三种项目布局中执行到正确的候选源码 | `project.py`、`runner.py`、pytest 适配器 | 检索和修复效果建立在可靠执行上 |
| 2 | 从陌生仓库检索相关函数与调用线索 | `retrieval/`、`tools.py` | 给修复循环提供可核查上下文 |
| 3 | 模型根据测试反馈决定查代码、提新补丁或结束，程序负责预算和验收 | `feedback.py`、`budget.py`、`workflow.py` | 为细粒度恢复提供步骤边界 |
| 4 | 跨进程继续任务，跨任务检索有来源的经验 | `memory.py`、恢复入口与图状态 | 为长期使用和记忆实验提供基础 |
| 5 | 在真实 Bug 上测量各模块收益 | `evals/manifest.py`、`run.py`、`summarize.py` | 决定发布版的默认策略 |
| 6 | 在目标仓库 CI 失败后调用同一套 CLI | `action.yml`、工作流、报告与使用文档 | 形成完整演示和接入方式 |

预计专注开发工时沿用进阶计划，合计 65 至 97 小时。每个阶段按下面的完成条件验收，实际模型用量和运行时间单独记录。

## 阶段 0 建立真实模型运行基线

完成后应能回答“模型读了哪些代码、提出了什么补丁、为什么成功或失败、花了多少调用”。真实模型运行需要配置可用模型与 API 密钥。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `src/ci_repair/schemas.py` | 新增 | 先定义 `RunManifest`、`ModelCallRecord`、`ToolEvent`、`ArtifactRef`。统一任务 ID、schema_version、时间、模型标识和证据引用格式，字段随后续阶段逐步补齐 |
| `src/ci_repair/artifacts.py` | 新增 | `create_run()`、`write_manifest()`、`append_event()`、`write_artifact()`、`publish_report()`。保存原日志、完整工具结果、候选 diff 和报告。大内容写文件，事件里保存摘要和引用，报告采用原子写入 |
| `src/ci_repair/model.py` | 修改 | 统一模型初始化与调用记录。每次请求保存 call_id、目的、状态、耗时、用量和错误。记录真实工具返回与格式解析失败，敏感值不进入日志 |
| `src/ci_repair/cli.py` | 修改 | 读入日志后调用产物层保存原文。开始请求前检查模型配置，任务创建后显示 task_id 和产物位置。原 `publish()` 调用迁入产物层 |
| `src/ci_repair/workflow.py` | 修改 | 各节点写开始、完成和失败事件。将已有验证结果和模型记录关联到同一个 task_id |
| `tests/test_artifacts.py` | 新增 | 验证报告被中途写入打断时不会损坏旧报告，事件可解析，密钥类字段被清理 |
| `tests/test_model.py` | 修改 | 保留现有工具回传测试，补充结构化结果为空和模型异常时的记录。单元测试用脚本模型，真实模型运行作为手动集成验证 |
| `README.md`、`.gitignore` | 修改 | 写明模型配置、产物位置和本地实跑步骤。运行目录继续由 `.gitignore` 排除 |

### 实现顺序

1. 定义最小记录结构，再实现产物写入。
2. 将 CLI、工作流和模型接到产物层，保持现有命令可用。
3. 把两个已有示例复制到专用演示目录并生成 Git 基线，执行真实模型修复。
4. 保存成功和失败证据，整理本阶段基线。

运行目录目标如下。

```text
<state-dir>/runs/<task_id>/
  manifest.json
  events.jsonl
  report.json
  artifacts/
    input/ci.log
    model/<call_id>.json
    tools/<event_id>.json
    candidates/<candidate_id>.diff
```

### 完成条件

- [ ] 运费与结算示例完成真实模型请求，留下实际结果。
- [ ] 至少一条完整实修记录，同时保留失败或错误候选分析。
- [ ] 原日志、每次工具调用和测试结果都有 task_id 可关联。
- [ ] 格式错误和请求失败产生明确记录，配置错误在调用前被发现。

本阶段学习模型集成、Function Calling、结构化输出与 Agent 可观测性。工具和步骤的详细记录是后续诊断、恢复和评测的共同依据。

## 阶段 1 项目接入与可靠执行

完成后应能确认“本次测试运行的是这个 commit 上的候选源码”。优先处理已经复现的 `src/` 导入指向原项目的问题。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `src/ci_repair/schemas.py` | 扩展 | `ProjectProfile`、`ProcessResult`、`TestRun`、`TestCaseResult`、`FailureEvidence`。记录命令参数、解释器、依赖指纹、导入来源、收集清单、逐阶段结果与完整日志位置 |
| `src/ci_repair/project.py` | 新增 | `load_project_config()`、`resolve_project_profile()`、`check_project()`、`prepare_candidate_environment()`。使用 `tomllib` 读配置，合并 CLI 覆盖值，诊断环境并准备候选运行方式 |
| `src/ci_repair/runner.py` | 新增 | `run_process()`、`terminate_process_tree()`。以参数列表启动进程，持续写日志，保留有界摘要，处理超时、返回码和进程树清理。首个实现用于本地自有项目 |
| `src/ci_repair/pytest_reporter.py` | 修改 | 在现有测试报告 hook 上增加用例收集清单、运行阶段和会话完成标记。保存候选内项目模块的加载来源，处理参数化用例、fixture 错误与 xfail |
| `src/ci_repair/pytest_adapter.py` | 修改 | 使用 runner 执行目标解释器，把 reporter 结果转换成 `TestRun`。保留 pytest 配置与声明的参数，分类正常失败、收集错误、缺依赖、未执行和超时 |
| `src/ci_repair/workspace.py` | 修改 | worktree 生命周期记录到产物层。清理失败独立报告，恢复和重试时识别残留目录，核对所有操作的目标目录和基线提交 |
| `src/ci_repair/verification.py` | 修改 | 基线和候选使用同一测试计划，比较收集、执行与状态。记录未执行用例和基线已有失败。将环境阻塞与业务断言失败分别返回 |
| `src/ci_repair/cli.py` | 修改 | 增加 `doctor --repo`、`--config`。把配置与显式参数合并后生成固定运行清单，`fix` 和 `doctor` 复用项目解析 |
| `examples/ci-repair.example.toml` | 新增 | 提供配置示例，包含工作目录、源码路径、解释器选择、pytest 参数、回归范围和超时。用户复制为目标仓库的 `.ci-repair.toml` |
| `tests/test_project.py` | 新增 | 覆盖参数覆盖顺序、平铺包、`src/` 包、子目录项目与环境缺失 |
| `tests/test_runner.py` | 新增 | 验证大量日志、超时、子进程回收和错误输出。只增加运行器需要承担的行为测试 |
| `tests/test_repair.py` | 修改 | 保留现有集成案例，增加继承环境导致导入旧代码、目标跳过和收集结果减少的回归 |

### 实现顺序

1. 项目配置与 `doctor`，先显示真实运行条件。
2. runner 与 reporter，生成可靠的执行证据。
3. 为平铺、`src/` 与子目录三种布局准备匹配环境。
4. 验收器读取新结果，报告明确的环境和业务结论。

源码路径调整需要考虑包的安装方式。依赖 editable install 的项目，在候选专属环境或项目声明的 runner 中安装该候选版本，并核对导入位置。编译扩展、pytest 插件和外部服务按项目配置提供，环境指纹写入清单。

### 完成条件

- [ ] `doctor` 能指出解释器、源码路径、实际 import 来源和测试计划。
- [ ] 三种布局均能复现错误并用参考补丁修复，证明候选源码被执行。
- [ ] 缺依赖、未复现、超时与断言失败有各自结论。
- [ ] 目标用例被跳过、收集遗漏或测试中断时，验收结论包含具体原因。
- [ ] 超时回收对应进程，用户当前工作目录中的编辑仍可继续使用。

本阶段学习工具执行环境、结构化 Observation、执行证据与状态一致性。Agent 的判断依赖这些外部结果的可信度。

## 阶段 2 代码 RAG 与跨文件定位

完成后，Agent 可以根据报错检索函数、调用线索与测试，再按证据继续查找。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `src/ci_repair/schemas.py` | 扩展 | `CodeChunk`、`RetrievalQuery`、`RetrievedEvidence`。包含 commit、path、symbol、父符号、行号、内容 hash、检索分数和召回来源 |
| `src/ci_repair/retrieval/__init__.py` | 新增 | 定义检索模块的最小公开入口 |
| `src/ci_repair/retrieval/chunks.py` | 新增 | `extract_chunks()`。使用 AST 切分函数、方法和模块级代码，保留 decorators、imports 和行范围。长块拆分、解析失败回退都记录原因 |
| `src/ci_repair/retrieval/index.py` | 新增 | `build_index()`、`load_commit_view()`。SQLite 存元数据，FTS5 存搜索文本，按内容 hash 复用块。提交视图处理修改与删除，schema 有版本 |
| `src/ci_repair/retrieval/search.py` | 新增 | `search_symbols()`、`search_bm25()`、`expand_references()`、`select_context()`。拆分标识符，精确命中与文本召回结合，去重后按 Token 预算选择片段 |
| `src/ci_repair/tools.py` | 新增 | 从 `model.py` 迁入已有工具定义，增加 `list_files`、`search_symbols` 和 `retrieve_code`。工具返回统一证据结构，限制到允许读取的提交视图 |
| `src/ci_repair/model.py` | 修改 | 用检索到的证据构建初始上下文，随后允许模型请求追加查询。保留读取原文工具，核查命中的行范围 |
| `src/ci_repair/workflow.py` | 修改 | 加入代码索引和检索步骤，保存查询、命中和进入上下文的片段 ID |
| `tests/test_retrieval.py` | 新增 | 真实代码夹具覆盖同名方法、嵌套函数、长函数、删除文件、跨 commit 更新和解析降级 |
| `tests/test_model.py` | 修改 | 验证模型接收到带来源的片段，能通过工具继续核查，并保存证据引用 |

### 第二轮再实现的检索内容

BM25 基线完成后，在 `retrieval/search.py` 加入 embedding 召回和 RRF 合并排名。`retrieval/index.py` 保存 embedding 模型 ID、维度与内容 hash。`project.py` 读取相关配置，`pyproject.toml` 和 `uv.lock` 只在确定所用集成后增加依赖。测试使用固定向量验证合并与版本匹配，真实召回效果用评测集测量。

### 实现顺序

1. AST 分块与提交视图，确保片段对应真实源码。
2. 精确符号、BM25 和 imports 扩展，形成可用检索。
3. 工具绑定与上下文选择，记录实际送入模型的内容。
4. 在根因标注案例上测量 Recall@K，再对照向量混合召回。

### 完成条件

- [ ] 同名函数能用文件、所属类或 imports 区分。
- [ ] 检索结果始终指向本次 commit，可用读取工具复核。
- [ ] 跨文件结算案例可以检索到调用方和运费逻辑。
- [ ] 保存字面搜索、BM25、可选混合检索的定位和修复对照。
- [ ] 报告包括上下文大小、查询开销和索引更新情况。

本阶段学习 RAG 的分块、索引、召回、上下文选择和效果评测。符号关系只扩展有依据的线索，动态调用关系在结果中说明不确定性。

## 阶段 3 测试反馈驱动的修复循环

以现有 `sequential` 策略为起点。候选被拒后，模型根据失败断言、堆栈和候选 diff 更新故障假设，在预算内决定继续查代码、直接提出新补丁，或给出理由并结束尝试。程序负责应用补丁、执行测试、判定验收和强制执行预算。首版可以复用现有的读文件、字面搜索和提交 diff 工具。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `src/ci_repair/schemas.py` | 扩展 | `Candidate`、`ValidationResult`、`RepairFeedback`、`BudgetState` 和结构化模型决策。候选记录稳定 ID、基线、diff hash、parent_id 和证据引用 |
| `src/ci_repair/feedback.py` | 新增 | `build_feedback()`。提取补丁应用错误、目标断言失败、回归断言与栈、缺失用例和环境阻塞，按重要性生成有界模型上下文，完整内容链接原始产物 |
| `src/ci_repair/budget.py` | 新增 | `check_budget()`、`reserve_call()`、`record_usage()`。管理模型、工具、候选、测试和总时长预算。调用失败与用量未知时保留状态，恢复核对调用记录 |
| `src/ci_repair/model.py` | 修改 | 单步调用接收最新验收反馈和工具结果，返回三类结构化决策：调用只读工具、提出新候选、带理由结束。无效格式最多修正一次，核验候选引用的源码位置 |
| `src/ci_repair/workflow.py` | 修改 | 拆分 `retrieve`、`model_step`、`execute_read_tools`、`propose`、`apply`、`test_target`、`test_regression`、`judge`。`sequential` 在 `rejected` 后把反馈交给 `model_step`，按模型选择执行下一步。程序负责检查预算、处理执行阻塞及结束已通过的任务。兼容 `batch` 和 `--patch` |
| `src/ci_repair/workspace.py` | 修改 | 依据候选 ID 和基线创建、检查、重建工作目录。应用前检查 diff，记录应用后的实际差异 |
| `src/ci_repair/verification.py` | 修改 | 分别执行目标、相关测试和最终回归，形成 `ValidationResult`。将每条失败证据交给反馈层 |
| `src/ci_repair/artifacts.py` | 修改 | 关联模型决策、候选及其父候选、完整失败证据和验收结果，记录模型主动结束的理由与程序触发的预算耗尽。候选 hash 重复时记录去重事件 |
| `tests/test_feedback.py` | 新增 | 锁定目标已通过但回归失败时，反馈确实含回归断言与栈，避免重复当前缺口 |
| `tests/test_workflow.py` | 新增 | 用同一份失败反馈驱动可控模型分别选择调用工具、直接提补丁和结束，检查对应路由。覆盖首次引入回归、读证据后修复通过，以及主动结束、格式错误、重复候选和预算耗尽 |
| `tests/test_model.py`、`tests/test_repair.py` | 修改 | 适配迁出的候选结构，继续通过现有集成案例，并补真实模型反馈修复的手动验证记录 |

### 实现顺序

1. 先用运费案例补齐回归反馈。第一份补丁让普通地区门槛用例通过，却把偏远地区的 25 元运费错误地变成 0 元。
2. 引入候选 ID、diff hash 和统一预算，关联候选、验证结果和反馈。
3. 接入模型决策，把只读工具结果送回模型，把新候选交给验收，把主动结束的理由写入报告。
4. 拆分并保存关键步骤，验证候选去重、停止条件、验收范围和已有 CLI 策略。

候选始终对同一原失败提交生成完整 diff，`parent_id` 表示这份候选参考了哪次尝试。模型主动结束时，报告记录理由和已有结果；没有通过验收的候选时，任务以 `rejected` 结束。预算耗尽和执行阻塞各有明确的结束原因。

### 完成条件

- [ ] 回归失败包含预期值、实际值、栈和被验证的 diff。
- [ ] 运费案例中，模型收到偏远地区用例的预期值、实际值和堆栈后，能选择继续读取相关代码，并引用该反例说明新补丁为何保留偏远地区优先收费的规则。新补丁通过目标和回归测试。
- [ ] 对同一份拒绝反馈，模型可以选择继续查代码、直接提新补丁或带理由结束。工具结果回到模型，主动结束后不再生成候选。
- [ ] 相同候选被识别，预算耗尽时有准确报告。
- [ ] 每个候选在相同基线与声明环境下由程序验证。测试执行阻塞时记录原因，通过验收后结束任务。
- [ ] 工具结果、模型提议和程序验收结论相互可关联。

本阶段学习 Agent 的反馈循环、结构化状态、条件路由和计算预算。多候选搜索是否值得使用，由阶段 5 的效果和成本决定。

## 阶段 4 任务恢复与长期记忆

完成后，用户可关闭进程再继续同一修复任务，也可在新任务中检索过去的有证据经验。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `src/ci_repair/memory.py` | 修改 | `initialize()`、`migrate()`、`save_experience()`、`search()`、`invalidate()`。增加经验来源、验证范围、失败类别、过期状态、最近核查时间 |
| `src/ci_repair/project.py` | 修改 | 生成稳定 repo_id，优先使用显式 ID 或去除凭据的规范化远端地址，本地仓库持久化生成标识 |
| `src/ci_repair/schemas.py` | 扩展 | `RepairExperience`、恢复信息与数据版本。旧 dict 报告通过明确的兼容读取升级，无法迁移时报告版本问题 |
| `src/ci_repair/workflow.py` | 修改 | 细粒度节点使用 checkpoint。恢复核对 commit、环境和候选 hash，完整步骤复用，未完成步骤重建后执行 |
| `src/ci_repair/budget.py` | 修改 | 根据已完成调用记录恢复预算，区分完成、失败与执行结果未知，记录重试的实际资源开销 |
| `src/ci_repair/artifacts.py` | 修改 | 检查恢复所需产物完整性，报告原任务与恢复运行的关联，避免复用缺失证据 |
| `src/ci_repair/cli.py` | 修改 | 丰富 `status`、`resume` 输出。增加经验查看、删除和禁用入口，保留已有 `--no-memory` |
| `tests/test_resume.py` | 新增 | 在工具后、应用后、测试中、测试后分别中断进程恢复，验证状态和产物一致性 |
| `tests/test_memory.py` | 新增 | 空库初始化、旧 schema 迁移、仓库过滤、失效经验和同报错异根因案例 |

### 实现顺序

1. 先稳定 repo_id、数据库初始化与迁移。
2. 在阶段 3 的步骤边界逐项验证恢复。
3. 补充经验来源、失败经验和失效处理，再改进排序。
4. 对记忆关闭、有效历史与误导性历史进行对照。

### 完成条件

- [ ] A 已淘汰时退出，恢复后准确继续 B。
- [ ] 进行中的测试重跑，已有完整结果的步骤正确复用。
- [ ] 目录位置改变后，能核对项目与 commit 再恢复。
- [ ] 每条经验能跳回来源任务和验收证据。
- [ ] 空库、迁移和失效记录有稳定结果，记忆误导案例有报告。

本阶段学习短期状态、持久化 checkpoint、跨任务经验和幂等恢复。已发送请求但结果未落盘的外部调用需要记录不确定性，后续重试计入实际用量。

## 阶段 5 真实 Bug 数据集与实验

完成后，应能用数据解释检索、失败反馈和记忆各自的效果，以及没有解决的错误类型。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `evals/manifest.py` | 新增 | 定义并校验 `BenchmarkCase`。包含仓库来源、坏版本、允许历史、环境配置、公开目标、最终回归、隐藏检查位置和案例划分 |
| `evals/cases/development.jsonl` | 新增 | 开发调试集，先收集能稳定复现的真实案例，记录出处、复现条件与排除原因 |
| `evals/cases/evaluation.jsonl` | 新增 | 固定评测集，按时间或故障族划分。完整版本覆盖至少三个仓库，目标 15 至 20 个真实 Bug |
| `evals/prepare.py` | 新增 | 准备故障提交、公开复现测试与运行环境，生成 Agent 可访问的提交视图。参考修复与隐藏检查保存在评测器目录 |
| `evals/run.py` | 修改 | 保留合成案例回归模式，增加真实案例和策略开关，保存环境失败与模型失败，按配置重复运行 |
| `evals/summarize.py` | 新增 | 计算复现率、独立解决率、误接受、Recall@K、回归拦截与资源开销。输出逐案例结果、分组表和失败分类 |
| `evals/README.md` | 新增 | 写清准备、运行、结果复跑、记忆划分及隐藏评测方式。实际结果附模型版本和预算 |
| `tests/test_evaluation.py` | 新增 | 用小型 Git 夹具验证参考答案不可读、隐藏检查参与独立判定、汇总分母与环境失败统计正确 |

### 实验顺序

1. 整理 5 个真实 Bug，先确认环境和失败都能重现。
2. 跑基础工具修复，保存错误类型。
3. 分别打开代码 RAG、回归反馈、历史记忆，使用同一评测集和预算上限。
4. 扩展到目标案例数，对波动明显的配置多次运行。
5. 分析成功与失败，决定发布版默认策略。

评测专用检查由外部评测器执行，参考修复与后续提交不进入 Agent 可读取范围。当前 `git_diff` 工具允许读取哪些历史也要在评测模式中固定。根因位置以人工核查与参考修复辅助标注，等价修复单独复核。

### 完成条件

- [x] 原故障、标准修复和隐藏检查能独立复现。20 个案例已通过本地校准。
- [x] 全部案例与可复现子集分别报告，环境失败保留在数据里。
- [x] 对照有统一预算上限，额外索引与测试开销单列。
- [x] 结果能关联到源码提交、模型版本、配置、轨迹和产物。case-04 已实际取得四组模型轨迹。
- [ ] 每项效果都填实际数字及分母，失败有可追踪原因。

阶段五的本地结果、单案例 DeepSeek 对照和统计口径见[真实故障评测说明](evals/README.md)。完整固定集的模型解决率与多轮波动仍待测量。

本阶段学习 Agent 评测、模块消融、数据泄漏控制和随机性分析。现有合成案例继续用于工程测试，真实解决率由独立评测取得。

## 阶段 6 CI 接入与发布演示

完成后，开发者可从本地启动，也可让目标仓库 CI 在测试失败后启动同一个修复流程，并下载补丁和报告。

### 文件与实现任务

| 文件 | 动作 | 需要写好的内容 |
| --- | --- | --- |
| `action.yml` | 新增 | 定义可复用 Action。输入包括仓库、失败 SHA、日志、项目配置、目标解释器与预算。安装工具使用独立环境，目标依赖使用项目声明的环境，输出 task_id 和报告位置 |
| `.github/workflows/ci.yml` | 修改 | 持续运行本项目的关键测试，覆盖 Windows 与 Linux，构建安装包并验证 CLI 入口 |
| `.github/workflows/ci-repair-demo.yml` | 修改 | 从单个示例演示升级为完整模型实修，记录被测 SHA，保留足够历史，保存和恢复声明的任务状态，上传产物 |
| `src/ci_repair/artifacts.py` | 修改 | 增加 `render_markdown_report()`，在已有 JSON 结果上生成可读报告，列出改动原因、证据、验收范围、残余失败与用量 |
| `src/ci_repair/cli.py` | 修改 | 为 CI 提供稳定退出码和产物输出，任务去重依据仓库、失败 SHA、目标测试和配置摘要 |
| `docs/usage.md` | 新增 | 本地首次运行、项目配置、doctor、恢复、错误排查和支持范围 |
| `docs/demo.md` | 新增 | 三段实测演示步骤，分别是跨仓库定位、回归反馈修订和进程恢复，引用实际任务报告 |
| `README.md` | 修改 | 安装、最小命令、工作流接入、实测结果和详细文档入口。所有效果数字引用阶段 5 的输出 |

### 实现顺序

1. 本地安装包和报告格式稳定后定义 Action 输入输出。
2. 在自己的测试仓库执行一次失败触发的远端模型修复。
3. 核对 CI 使用的 SHA、日志、检查范围和上传产物。
4. 验证跨 runner 状态恢复，整理发布与演示文档。

创建修复 PR 可在接入稳定后增加显式命令。实现时再次核对目标分支版本，提交内容链接相同 SHA 上的验收报告。

### 完成条件

- [ ] 从另一个支持的项目调用 Action，真实模型能够生成并验证补丁。
- [ ] CI 使用的失败 SHA 与报告基线一致，覆盖 PR 合并版本的情况。
- [x] 本地与 CI 调用同一业务流程，输出一致结构。
- [x] 状态恢复和产物下载有实际运行记录。shipping 任务在新 runner 上校验通过并继续执行。
- [ ] 其他开发者能按文档跑通支持配置下的演示。

本阶段学习 CI 自动化、可复用交付、异步任务恢复和面向开发者的结果呈现。

## 目标源码结构

新增模块按实际依赖逐阶段落地。现有测试文件继续承担集成验证，新增测试围绕已确认的错误与关键状态转换。

```text
ci-repair-agent/
  src/ci_repair/
    cli.py
    schemas.py
    artifacts.py
    project.py
    runner.py
    pytest_adapter.py
    pytest_reporter.py
    workspace.py
    verification.py
    tools.py
    model.py
    workflow.py
    feedback.py
    budget.py
    memory.py
    retrieval/
      __init__.py
      chunks.py
      index.py
      search.py
  tests/
    test_model.py
    test_repair.py
    test_artifacts.py
    test_project.py
    test_runner.py
    test_retrieval.py
    test_feedback.py
    test_workflow.py
    test_resume.py
    test_memory.py
    test_evaluation.py
  evals/
    manifest.py
    prepare.py
    run.py
    summarize.py
    cases/
      development.jsonl
      evaluation.jsonl
    README.md
  examples/
    shipping/
    checkout/
    ci-repair.example.toml
  docs/
    usage.md
    demo.md
  action.yml
  .github/workflows/
    ci.yml
    ci-repair-demo.yml
```

`pyproject.toml` 与 `uv.lock` 随实际依赖和打包要求更新。CLI 兼容、历史数据版本升级与现有集成案例在各阶段验收时一起检查。

## 第一批具体开发任务

从以下顺序开始，每个任务先完成代码和针对性验证，再记录结果。

1. 创建 `schemas.py`，定义阶段 0 的运行、调用与证据结构。
2. 创建 `artifacts.py` 和 `tests/test_artifacts.py`，建立完整日志和原子报告输出。
3. 修改 `cli.py`、`model.py`、`workflow.py`，把现有流程接入证据记录。
4. 配置模型后，运行两个示例并保存真实基线。
5. 创建 `project.py`，加入配置解析和 `doctor`，为已经复现的导入路径问题建立回归案例。

每次交付说明本次写好的文件、接口输入输出、运行命令、实际结果与对应 Agent 知识。阶段 0 的模型实跑需要可用密钥；阶段 1 的配置、运行器与回归案例可以独立推进。

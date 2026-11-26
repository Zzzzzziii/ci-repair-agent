# 在自己的项目使用 CI Repair Agent

适用范围是 Git 仓库中可重复运行的 Python/pytest 业务逻辑故障。先在目标环境安装项目依赖和 pytest。Agent 自身需要 Python 3.12 或以上。保留 CI 使用的提交和完整 pytest 输出，摘要里的 `FAILED path::test_name` 用于选择目标测试。

## 第一次本地运行

在 Agent 源码目录运行 `uv sync --frozen`。以下 PowerShell 示例假设业务项目在相邻目录 `shop`，日志为本目录的 `ci.log`。将 `CI_REPAIR_MODEL` 设为账号使用的完整 `deepseek:模型名`，在当前环境提供 `DEEPSEEK_API_KEY`。

```powershell
$target = (Resolve-Path ../shop).Path
$python = Join-Path $target '.venv/Scripts/python.exe'
uv run ci-repair doctor --repo $target --python $python
uv run ci-repair --state-dir .ci-repair fix --repo $target --python $python --log ci.log --regression tests --strategy sequential
```

命令会打印任务 ID。打开状态目录下的 `runs/<任务 ID>/report.md` 阅读报告，打开同目录的 `report.json` 查看每次工具调用、测试和候选补丁。结果为 `accepted` 时下载 `fix.diff`，在同一 SHA 上用 `git apply --check` 核对后审阅。验收范围由目标用例和声明的回归测试决定。

如项目使用 src 布局，可把 [TOML 示例](../examples/ci-repair.example.toml) 复制为目标仓库根目录的 `.ci-repair.toml`。其中 `cwd` 和 `source_paths` 相对仓库根目录，pytest 选择器相对 `cwd`。命令行参数覆盖项目默认值。为单体仓库内的子项目指定实际测试入口和回归范围。

## 从 GitHub Actions 接入

在**可信代码**的测试作业里使用下面的步骤。将 `<agent-commit>` 换成已审阅的 Agent 完整提交 SHA。示例假设前面已 checkout 当前被测版本、setup-python 3.12 并安装业务依赖。PR 测试若检出 GitHub 生成的合并提交，就使用实际 `HEAD` 记录该次测试的 SHA。checkout 设置 `persist-credentials: false`，作业使用 `contents: read`。目标测试和补丁在调用方 runner 执行，只在自己信任的仓库和分支提供模型 Secret。

```yaml
- id: tests
  shell: bash
  run: |
    sha=$(git rev-parse HEAD)
    echo "sha=$sha" >> "$GITHUB_OUTPUT"
    echo "GITHUB_SHA=$sha" > ci.log
    set +e
    python -m pytest -q tests >> ci.log 2>&1
    rc=$?
    echo "rc=$rc" >> "$GITHUB_OUTPUT"
    exit "$rc"
- id: repair
  if: failure() && steps.tests.outputs.rc == '1'
  uses: Zzzzzziii/ci-repair-agent@<agent-commit>
  env:
    DEEPSEEK_API_KEY: ${{ secrets.API_KEY }}
  with:
    repository: .
    failed-sha: ${{ steps.tests.outputs.sha }}
    log: ci.log
    target-python: python
    regression: tests
    model: ${{ vars.CI_REPAIR_MODEL }}
    budgets: '{"model_calls":12,"candidates":3,"tests":16}'
- uses: actions/upload-artifact@v4
  if: always() && steps.repair.outputs.task-id != ''
  with:
    name: ci-repair-report
    include-hidden-files: true
    path: .ci-repair/runs/
```

原测试步骤保留失败状态，补丁作为可下载的审查材料交付。Action 单独创建工具虚拟环境；`target-python` 必须能运行目标测试。可选的 `config` 指定 TOML，`regression` 每行一个选择器。输出有 `task-id`、`status`、`report`、`markdown` 和验收通过时的 `patch`。模型和测试额度均可通过 `budgets` 设置。

## 重试与恢复

本地运行 `ci-repair --state-dir .ci-repair status <任务ID>` 看下一步，运行 `ci-repair --state-dir .ci-repair resume <任务ID> --repo <项目路径>` 恢复。CLI 的 `--reuse-task` 和 Action 会依据仓库身份、被测提交、目标、有效配置和初始补丁确定任务 ID。同一状态目录内，已完成任务可复用，未完成任务从持久化检查点继续。默认的本地 `fix` 每次创建独立任务。

跨 runner 时，先结束当前任务进程，上传 `checkpoints.sqlite*`、`repairs.sqlite*` 和整个 `runs/`，下一作业恢复到相同的状态目录。两边使用相同版本和路径的目标解释器、依赖、测试设置、相同仓库和 SHA。只从自己的可信作业恢复检查点。排队耗时也计入本次任务的总时限。[完整两作业示例](../.github/workflows/ci-repair-demo.yml)

## 常见结果

| 现象 | 查看与处理 |
| --- | --- |
| 找不到失败用例 | 打开日志确认有完整的 pytest 节点名。本地也可直接传 `--test path::name` |
| SHA 冲突 | 对照日志的 `GITHUB_SHA=` 与原测试作业的 `git rev-parse HEAD`，恢复同一 checkout |
| 环境或导入错误 | 用同一个 `target-python` 运行 doctor 和目标用例，核查工作目录和源路径 |
| 补丁被拒绝 | 阅读报告里对应候选的目标断言、回归失败与下一轮反馈 |
| 中断后校验失败 | 核对还原的 SQLite、运行目录、依赖和原始日志是否来自同一任务 |

退出码 `0` 表示验收通过或按要求暂停，可读 `status` 区分两者。`1` 表示候选被拒绝，`2` 表示输入、环境或执行问题。

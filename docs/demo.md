# 三个演示场景

在 GitHub 的 **Actions → CI Repair Agent baseline → Run workflow** 选择功能分支并启用 `feedback_demo`。仓库 Secret `API_KEY` 提供 DeepSeek 密钥，Variable `CI_REPAIR_MODEL` 指定模型。一个工作流产生以下三段证据。

| 场景 | 工作流做什么 | 打开哪个文件 |
| --- | --- | --- |
| 跨仓库定位 | checkout more-itertools 的固定故障提交，另提交两个公开用例，记录这个测试提交，调用同一 Action 分析空迭代器故障 | `repair-upstream` 内的 `ci-error.log`、`report.md`、`artifacts/tools/` |
| 回归反馈 | 在 shipping 示例输入一个会破坏已有运费规则的初始补丁，目标测试和回归测试分别执行，保存拒绝原因 | `repair-shipping` 内的 `report.json` 和 `artifacts/feedback/` |
| 跨 runner 恢复 | 新作业下载 checkpoint，核对提交和证据，继续同一个任务，让模型根据第一轮结果提出修改 | `shipping-resumed` 内的 `artifacts/recovery/`、`events.jsonl` 和最终报告 |

核对两份 shipping 报告的任务 ID 和被测 SHA。恢复记录的 `completed_candidates` 应为 1；后续候选的验证结果说明第二轮改动是否通过回归。跨仓库示例的被测 SHA 包含演示用例，可通过其父提交回溯到[上游故障版本](https://github.com/more-itertools/more-itertools/commit/5d946b3590bfe92f1465c1b9b9830dd434745c84)。

## 首次远端运行记录

在 `6e43d2b` 上，shipping 的初始补丁被回归测试拦截。新 runner 下载了检查点并恢复任务 `7d1110aa-f928-57fb-9b5d-c083a6ce7346`。恢复记录显示已验收过 1 个候选、下一节点为 `retrieve`、校验通过。随后模型两次未给出可执行决策，以 `invalid_decision` 结束。上游示例也复现了预期的 1 失败和 1 通过，模型在同一协议检查处被拒绝。[首次演示及三份报告](https://github.com/Zzzzzziii/ci-repair-agent/actions/runs/45829482474)

## 修正工具调用后的实测

在 `84f1c32` 上，模型 `deepseek-flash` 完成了两个修复，三个 GitHub 作业均通过。工作流由 Agent 仓库发起，分别以 more-itertools 的独立 checkout 和本仓库的运费示例作为目标。[完整运行记录](https://github.com/Zzzzzziii/ci-repair-agent/actions/runs/45843627493)

| 证据 | 实际结果 |
| --- | --- |
| 上游空输入 | 被测提交 `e84d8ff` 的日志与报告 SHA 一致。模型读取源码后为 `interleave_evenly` 加入空输入处理，2 次模型调用、1 个候选，两个公开测试通过。下载同一份 `fix.diff` 后，阶段五 Docker 裁判重新运行公开目标、公开回归和隐藏用例，3/3 通过。[上游报告和补丁](https://github.com/Zzzzzziii/ci-repair-agent/actions/runs/45843627493/artifacts/15129502479) |
| 运费第一轮 | 给定候选使普通地区满 200 元免运费，却让偏远地区同金额也免运费。Agent 记录失败的回归用例，拒绝补丁并暂停。[第一轮报告](https://github.com/Zzzzzziii/ci-repair-agent/actions/runs/45843627493/artifacts/15128672960) |
| 运费恢复后 | 两台 runner 的任务 ID 均为 `139f7023-e72a-5866-a1ef-ae7ffbfce7e3`，被测 SHA 均为 `84f1c32`。新 runner 恢复时已有 1 个已验证候选。模型保留偏远地区优先收费的判断，把普通地区门槛从 `>` 改为 `>=`。第二个候选通过全部 4 个运费用例，任务共用 3 次模型调用。[恢复证据和最终补丁](https://github.com/Zzzzzziii/ci-repair-agent/actions/runs/45843627493/artifacts/15128648156) |

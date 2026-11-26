# 三个演示场景

在 GitHub 的 **Actions → CI Repair Agent baseline → Run workflow** 选择功能分支并启用 `feedback_demo`。仓库 Secret `API_KEY` 提供 DeepSeek 密钥，Variable `CI_REPAIR_MODEL` 指定模型。一个工作流产生以下三段证据。

| 场景 | 工作流做什么 | 打开哪个文件 |
| --- | --- | --- |
| 跨仓库定位 | checkout more-itertools 的固定故障提交，另提交两个公开用例，记录这个测试提交，调用同一 Action 分析空迭代器故障 | `repair-upstream` 内的 `ci-error.log`、`report.md`、`artifacts/tools/` |
| 回归反馈 | 在 shipping 示例输入一个会破坏已有运费规则的初始补丁，目标测试和回归测试分别执行，保存拒绝原因 | `repair-shipping` 内的 `report.json` 和 `artifacts/feedback/` |
| 跨 runner 恢复 | 新作业下载 checkpoint，核对提交和证据，继续同一个任务，让模型根据第一轮结果提出修改 | `shipping-resumed` 内的 `artifacts/recovery/`、`events.jsonl` 和最终报告 |

核对两份 shipping 报告的任务 ID 和被测 SHA。恢复记录的 `completed_candidates` 应为 1；后续候选的验证结果说明第二轮改动是否通过回归。跨仓库示例的被测 SHA 包含演示用例，可通过其父提交回溯到[上游故障版本](https://github.com/more-itertools/more-itertools/commit/5d946b3590bfe92f1465c1b9b9830dd434745c84)。

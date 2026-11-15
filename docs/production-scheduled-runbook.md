# JobRadar 每日采集上线说明

## 运行方式

GitHub Actions 在默认分支上按 UTC `17 1 * * *` 触发，对应北京时间每天 09:17。GitHub 调度繁忙时可能延迟。现有六个来源与五个试点来源共用已验收的采集适配器。定时路径使用默认分页和详情预算，不扩页，也不扫描历史岗位链接。遇到 robots 或访问限制立即停止该来源，同一来源累计三次异常也停止。

| 仓库变量 | 未设置或 `false` | `true` |
|---|---|---|
| `JOBRADAR_ENABLE_SCHEDULED_RUN` | 定时事件跳过采集和数据库步骤 | 定时采集并尝试生产只读预检 |
| `JOBRADAR_ENABLE_SCHEDULED_WRITE` | 跳过定时写入 | 只有定时采集、只读预检、历史参考和全部写入保护通过时才可新增 |

生产继续使用 `jobradar-production` 环境内已有的 `JOBRADAR_PROD_JOBS_DSN` 和 `JOBRADAR_PROD_JOBS_CA_PEM`，以及仓库变量 `JOBRADAR_PRODUCTION_PROJECT_REF`。采集步骤没有数据库凭据。数据库步骤校验项目引用、`verify-full`、实际会话角色和 RLS，沿用现有权限检查。当前无需执行额外生产迁移。

## 首次审核与试运行

1. 将审核后的代码手动发布到默认分支。到仓库 **Settings → Secrets and variables → Actions → Variables**，确认两个定时变量均为空或 `false`。
2. 打开 **Actions → JobRadar production jobs → Run workflow**，选择默认分支，只勾选 `inspect_scheduled`。确认 `collect` 和 `preflight` 的状态，`write`、`scheduled_write` 应跳过。下载 `production-collection` 和 `production-preflight`，核对来源警告、正式库基线、拟新增岗位、真实发布日期、官网投递入口和待核验原因。以运行记录验证 TLS、RLS 和只读事务。任何失败都保留报告并停止推进。
3. 通过后只将 `JOBRADAR_ENABLE_SCHEDULED_RUN` 设为 `true`，保持写入变量关闭。至少观察一个更早的北京时间日期生成的成功 `production-scheduled-reference`，再观察下一天是否正确使用历史参考、跳过正式写入。若需要回滚验证，可用给采集账号配置的独立 staging 环境和既有安全凭据，对同一份合格采集报告执行下方测试命令。保留完整的岗位快照检查和现有 A/B 隔离验收。
4. 确认两天及以上观察中出现的候选质量、语义重复和来源波动，并检查 **Settings → Environments → jobradar-production** 的默认分支限制和审批设置。如果当前环境要求每次人工审批，它会挡住真正无人值守运行。审批设置由仓库所有者在最终上线审核时决定。仓库所有者审核通过后才将 `JOBRADAR_ENABLE_SCHEDULED_WRITE` 设为 `true`，当天结束核对实际新增、原有岗位数量及重复重放结果。
5. 若发现异常，把 `JOBRADAR_ENABLE_SCHEDULED_WRITE` 设为 `false`，然后检查并取消尚未完成的写入运行。若也要暂停采集，把 `JOBRADAR_ENABLE_SCHEDULED_RUN` 设为 `false`。开关应用于未来运行，已经提交的新增记录需要单独核对。

首次只读预检若显示历史积压超过动态上限，继续保持定时写入关闭。先逐条审核候选，核对重复、资格和仍可投递的证据，并用现有人工流程分批处理其允许的常规来源。仍有超限时，需要单独审核试点来源积压的处理方案。随后重跑完整只读预检；定时流程不会自行截取前几条或放宽上限。

```powershell
python -B tools/production_sync.py --verify-staging --scheduled-sources --report .\collection.json --output .\staging-scheduled-rehearsal.md
```

先在用于验收的 PowerShell 会话中载入测试项目的 `JOBRADAR_JOBS_DSN`、`JOBRADAR_JOBS_CA_FILE` 和 `JOBRADAR_STAGING_PROJECT_REF`。报告应来自最近两小时的 `--scheduled` 采集。此命令要求 staging 项目引用和 staging 专用采集角色，测试写入会回滚。SQL 会话和报告均只接触公开岗位。私人数据隔离继续由已验收的 A/B 登录路径和 RLS 保护。

## 批量保护与岗位推荐

- 检查来源、真实职位标识、企业、岗位、工作地及原始详情证据。只掌握公告、同名同城但批次或岗位身份不清楚、以及发布超过 60 天仍无开放证据的候选先留在报告里。详情证据充分但部分任职条件未知时可以作为待核验岗位新增。
- 来源警告、来源未完成、明确失效岗位或候选异常增长均中止本批。候选绝对上限为 80，自动新增上限是 8 与 `max(2, 当前岗位总数 ÷ 4 取整)` 中的较小值。基线为 27 条时最多自动新增 6 条。
- 只写入新岗位。写入前重新对比完整基线，在事务内重放同一批并核对新增数为零。所有历史公开岗位字段必须保持原值。已有岗位建议更新仍在报告中显示，留给独立人工流程审核。
- 个性化继续在本人登录后执行。已确认投递的岗位移出未投递机会列表，未确认的保留未知。公开采集报告里的拟新增只代表公共库首次收录，与个人是否投递是两回事。
- 单次源异常可等待下一次运行。若同一来源连续三次中止或固定链接已明确失效，应人工检查访问规则、详情地址与配置，再审核修复或降频，不扩大无效分页。对应 GitHub Actions 作业失败及报告作为告警入口。

## 30 天观察

采集、只读预检和写入报告作为 Actions Artifacts 保留 35 天。下载并按每日运行分目录解压 `production-collection`、`production-preflight`、`production-scheduled-write`。建议每个自然日只纳入一次正式定时运行的 artifacts，手动演练另存。

```powershell
python -B tools/scheduled_observation.py D:\JobRadarReports\scheduled
```

汇总按北京时间滚动 30 天，分来源对稳定公共岗位 ID 去重，区分只读拟新增、事务实际新增、有官网申请入口。每日报告另有来源返回数、重复、待核验和暂缓更新。观察覆盖天数和失败天数单独显示。岗位源发布日期由来源提供，可以为空，数据库首次发现时间和抓取时间各自保留。观察文件只使用公共岗位数据，如以后增加邮件提醒，可用已提交运行的公共岗位摘要作为触发条件，个人匹配与收件人设置由独立的、经授权的流程处理。

参考 [GitHub Actions 定时事件说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) 和 [构建产物保留说明](https://docs.github.com/en/actions/tutorials/store-and-share-data)。公开仓库长期无活动时，GitHub 可能停用定时工作流，观察时应同时核对计划日期和实际运行记录。

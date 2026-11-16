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
2. 打开 **Actions → JobRadar production jobs → Run workflow**，选择默认分支，只勾选 `inspect_scheduled`。确认 `collect` 和 `preflight` 的状态，`write`、`scheduled_write` 应跳过。下载 `production-collection` 和 `production-preflight`，核对来源警告、正式库基线、拟新增岗位、真实发布日期、官网投递入口和待核验原因。权限及来源均通过时，积压超限的只读作业也会成功，报告须明确显示「写入阻止」，JSON 的 `write_gate` 为 `blocked_backlog`。其他安全检查失败时流程报错并保留报告。
3. 保持写入变量关闭，可单独将 `JOBRADAR_ENABLE_SCHEDULED_RUN` 设为 `true` 观察。定时只读预检只要完成且检查结果为 `eligible` 或 `blocked_backlog`，就可保存当天的 `production-scheduled-reference`。下一天使用它检验来源返回量和候选量是否异常，完整积压名单仍随报告保存。缺少历史参考或写入条件不满足时跳过写入。
4. 确认两天及以上观察中出现的候选质量、语义重复和来源波动，并检查 **Settings → Environments → jobradar-production** 的默认分支限制和审批设置。如果当前环境要求每次人工审批，它会挡住真正无人值守运行。审批设置由仓库所有者在最终上线审核时决定。仓库所有者审核通过后才将 `JOBRADAR_ENABLE_SCHEDULED_WRITE` 设为 `true`，当天结束核对实际新增、原有岗位数量及重复重放结果。
5. 若发现异常，把 `JOBRADAR_ENABLE_SCHEDULED_WRITE` 设为 `false`，然后检查并取消尚未完成的写入运行。若也要暂停采集，把 `JOBRADAR_ENABLE_SCHEDULED_RUN` 设为 `false`。开关应用于未来运行，已经提交的新增记录需要单独核对。

## 积压岗位的人工处理和 staging 验收

1. 保持定时写入关闭。审核最新全来源报告，逐条确认企业、独立职位编号、城市、不同批次、真实发布日期、开放申请和现有库是否已收录。优先检查本人登录后仍未投递且资格对得上的记录。证据缺失、明显不适合或已有保留记录的岗位列为暂缓或排除。审批清单只保留公共岗位 ID、原始链接、理由、核验时间、报告 SHA-256 和批准批次，不含账号和投递信息。
2. 每次只批准当前动态上限以内的 ID。正式库基线为 27 条时单批最多 6 条。选定 ID 前仍对照完整采集和前一日参考，防止过滤后掩盖来源异常、可疑同岗或突增。没有达到质量要求时，允许 0 条入库。
3. 在 `jobradar-staging` 使用受限角色，执行下面的回滚演练。使用两小时内的完整 `--scheduled` 报告，以及更早的北京时间采集日生成的参考。每个经审核的 ID 单独加一个 `--reviewed-id` 参数。分别验证合格子集、已有或同名疑似重复 ID、超上限子集，以及不含某个来源的报告。合格子集事务内首次新增指定条数，重放新增 0、更新 0，事务结束后公共库快照应逐项一致。其他情况在写入前停止。若 staging 已包含真实候选，用明确的模拟候选补测首次插入。
4. 用测试账号或既有审计方式复核 A/B 原有记录；采集角色只核对公开岗位。等 staging 回滚验收通过，再审核生产人工入口。生产人工入口应绑定经过审批的 ID 清单与完整报告摘要，仅由默认分支手动触发，只走受限角色，在事务内重算计划及动态上限，沿用现有写入和回滚保护，每批完成后重新只读校验。这版 `--reviewed-id` 只实现 staging 回滚，当前生产工作流不能按人工 ID 清单提交批次。
5. 人工处理后跑一次新的完整只读预检。只有余量在上限内、前日参考和写入保护都通过、遗留不合格候选已有处理决定时，再参加最终开关审核。对反复出现的已拒绝岗位，先落实按来源和稳定 ID 记录的入库排除规则。规则须在全量来源和增长检查之后应用，保留原始报告，也需单独审核；这版未加入生产排除清单。若积压依然超限，继续生成只读观察并保留明细。

```powershell
$reviewedJobId = Read-Host '输入已审核的公共岗位 ID'
python -B tools/production_sync.py --verify-staging --scheduled-sources --report .\collection.json --previous-report .\previous.json --reviewed-id $reviewedJobId --output .\staging-scheduled-rehearsal.md
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

汇总按北京时间滚动 30 天，分来源对稳定公共岗位 ID 去重，区分只读拟新增、事务实际新增、有官网申请入口。每日报告另有来源返回数、重复、待核验和暂缓更新。覆盖天数、因积压阻止写入的天数与真正失败的天数分别显示。岗位源发布日期由来源提供，可以为空，数据库首次发现时间和抓取时间各自保留。观察文件只使用公共岗位数据，如以后增加邮件提醒，可用已提交运行的公共岗位摘要作为触发条件，个人匹配与收件人设置由独立的、经授权的流程处理。

参考 [GitHub Actions 定时事件说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) 和 [构建产物保留说明](https://docs.github.com/en/actions/tutorials/store-and-share-data)。公开仓库长期无活动时，GitHub 可能停用定时工作流，观察时应同时核对计划日期和实际运行记录。

## 每日新发现发布到网站

1. 审核并手动提交、推送变更。仓库 **Settings → Pages → Build and deployment → Source** 继续选择 **GitHub Actions**。保留 `JOBRADAR_ENABLE_SCHEDULED_RUN=true`、`JOBRADAR_ENABLE_SCHEDULED_WRITE=false`。
2. 在 **Actions → Deploy JobRadar Pages → Run workflow** 勾选 `preview_only`，可先审核手动构建的白名单和 CSP。这个选项只构建产物。取消勾选后可按原流程手动发布前端，手动发布沿用网站上已有的候选和更新时间。第一次发布若尚无日报，会在首个成功定时批次后自动出现数据。
3. 等下一次北京时间 09:17 采集后，在 **JobRadar production jobs** 核对 `collect` 和 `preflight`，并确认写入任务跳过。只读预检显示 `blocked_backlog` 或 `blocked_terminal` 时仍可发布公开候选，这两个结果均禁止写入。缺少合格产物、数据为空、采集失败或格式异常时，站点继续提供上次成功结果。**Deploy JobRadar Pages** 的自动记录只响应本仓库默认分支的成功定时运行。
4. 用自己的测试账号在网站选择 **每日新发现**，检查实际更新时间、匹配和来源链接。新增的收藏、投递和备注只进自己的 `manual_jobs`，若已存在私人记录则更新该记录。重复点击和同一浏览器多个标签页的操作受客户端锁和重新读取保护。现有表没有“账号＋岗位链接”的唯一约束，两台设备在同一瞬间首次保存仍可能各创建一条；页面发现多条候选关联时会提示核对。

首次发现按 URL 对应的公开岗位 ID 记录在日报历史中。上线前未出过日报的历史岗位可能在首份日报显示为首次纳入。该日期与来源真实发布日期独立，未知发布日期保持未知。

日报分别记录原公告和可靠提取的职位申请链接。页面以近 7 天的独立申请证据确认可投递。来源发布时间超过 30 天且缺乏近期证据的记录归为历史线索。已截止或已知失效的岗位留在折叠区，后续缺席日报时仍保留其公共历史和原有私人关联。

Pages 生成日报时，每次最多对 6 个白名单内的公开申请地址尝试核验。同一地址三天内复用核验结果。核验遵守 robots 和限速，限制响应大小，并禁止跟随跳转或访问非公网 IP。403、429、超时、验证码及交互式页面仅表示暂不能确认。单条入口核验异常不阻断日报，原有整批来源健康检查仍然生效。

自动工作流只消费同仓库的公开只读采集产物。Pages 使用 GitHub 提供的站点地址读上次已部署日报，浏览器从同源读取最新文件。公开文件没有账号、投递、备注或数据库密钥。见 [GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

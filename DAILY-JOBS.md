# 每日公共岗位采集

## 本地只读试跑

在 Windows PowerShell 中进入 `D:\JobRadar` 后运行下面的命令。采集只读取公开页面，把结果与本地 27 条历史岗位对照并输出报告。

```powershell
$jobRadarPython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $jobRadarPython -X utf8 -B tools/collect_jobs.py --baseline jobs.json --output dist/daily-final.json
& $jobRadarPython -B -m unittest discover -s tests
node --test tests/test_phase1.cjs tests/test_cloud.mjs tests/test_rls.mjs
```

报告的 `newly_published_7d` 依据招聘来源的发布日期。首次发现时间保存在每条岗位的 `first_seen`，后续更新保留原值。`new` 和 `first_discovered_against_baseline` 只描述相对所用快照首次出现的记录。`updated` 表示已有链接的资格证据或招聘状态发生变化。`duplicates` 包含无变化的已知岗位和证据充分的重复发布。没有基线时，报告只提供本轮候选，无法确认历史新增。比较生产库须以获批的写入环境为准。`same_title_unconfirmed` 统计保留的同名线索，批次、编号、发布日期、截止日冲突或缺少证据的条目保留。

已知截止日、明确停止招聘或历史详情返回 HTTP 404/410 可用于标注失效。登录限制、验证码、限流、超时和结构异常只记警告。历史复核每次最多尝试六条。页面显示来源发布日期和超过 30、60、90 天的标记，较旧且在招状态未明的岗位会降序。HR 活跃时间、未知发布日期或在招状态均单独标记。

国家大学生就业服务平台按城市和岗位关键词查询。对外经贸大学就业网当前接入的是配置中的公告。强生官网接入中国招聘页面及经核对的职位详情。后两者覆盖范围有限，不能代表各自的全量岗位。采集不使用账号求职配置，网页仍按当前登录者的配置匹配。招聘原文缺少的信息在报告中留空。

## 手动验收测试库

1. 在 GitHub Desktop 审查本地差异。确认后由维护者提交和推送到默认分支。`staging-sync.yml` 的模拟验收只允许默认分支触发，测试凭据保留在 `jobradar-staging` 环境。Pages 发布工作流单独手动触发。
2. 写入前，在 Supabase 后台确认项目是 **jobradar-staging**。在 SQL Editor 运行下方只读查询，将 51 条原岗位 ID、链接和首次发现时间保存到本地作为比对基线。验收期间保持其它采集任务暂停。
3. 打开 **Actions → JobRadar staging sync acceptance → Run workflow**，选择默认分支，保持 `real_collection` 未勾选。查看 `staging-acceptance` 产物，确认专用采集角色、私人表拒绝访问、两条受保护岗位和模拟写入回滚均通过。重新查询时，原岗位数量和基线应一致。
4. 模拟验收通过后再次手动运行，勾选 `real_collection`。核对 `staging-collection` 的来源和警告，读取 `staging-write` 中的实际新增、更新、重复、待核验数量。同一报告重放应新增 0、更新 0。报告里的待核验属于采集质量统计，个人匹配另计。
5. 写入后再次运行只读查询，确认原 51 个 ID 仍在且链接、首次发现时间不变。对照此次本地发现的四条候选 ID，逐条确认是否入库。礼来 `6ddf35289658b4bb`、ASML `d9dd37ca8b6b3a7d`、中建东方装饰 `fdea202336ae24e7`、强生高级法务 `814cb401e89b49e2`。官网状态会变化，实际新增数量以云端报告和数据库为准。
6. 在本地启动新版测试预览，分别登录 A、B。清空搜索和发布日期过滤，分别核对「推荐岗位」「待核验岗位」「可尝试岗位」，保留各账号已保存的其他筛选条件。在每个视图的开发者工具控制台运行 `document.querySelectorAll('#jobs article').length`，得到该视图实际展示的岗位数，包括折叠组内的原记录。核对匹配理由，以及已有的收藏、投递、备注和面试。验收报告只汇总数量，不贴私人字段。

```sql
select id, url, first_seen from public.jobs order by id;
```

### 本机新版测试预览

在 `D:\JobRadar` 运行下列命令，将占位符改为测试项目的 Project Ref。打开 [本机测试页](http://127.0.0.1:8002/job-radar/)。预览读取独立的 `.env.staging`，会拒绝和正式 `.env` 重复的项目或密钥。

```powershell
$jobRadarPython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $jobRadarPython -X utf8 -B tools/pages.py preview-staging --project-ref '测试项目的 Project Ref'
```

正式 Pages 发布和定时采集在测试库验收后分别审批。

## 官方来源扩展

`tools/collect_jobs.py` 默认运行所有已配置来源。可重复传入 `--source` 单独验证来源，例如：

```powershell
python -B tools/collect_jobs.py --source lilly --source asml --source hebut --source jnj --output dist/source-expansion/official-first-batch.json
```

- 礼来使用中国官网公开列表，最多读取五页，并从职位详情的 JobPosting 获取真实发布日期。ASML 和河北工业大学就业网只回查配置中列出的已知官方详情。强生继续使用中国官网加已知详情。
- 以上入口只读取公开招聘数据。ASML 的筛选参数路径受 robots.txt 限制。高校校园招聘页可显示岗位信息，但其登录投递控件不足以确认仍在招聘。
- 报告只记录公开岗位字段。收到 401、403、429 或 robots 拒绝时，停止该来源。未知薪资、团队人数、发布日期或投递状态按未知处理。
- 验收时先跑只读采集，用报告核对来源、岗位编号、日期、状态和重复情况。再手动跑 staging 模拟验收，核对保留的岗位 ID，然后审批测试库的增量写入。前端匹配验证通过后，另行审批正式站发布。


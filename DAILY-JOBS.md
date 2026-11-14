# 每日公共岗位采集

## JobRadar 2.0 正式增量写入验收

新流程复用现有公开来源和去重规则。采集阶段无数据库凭据，生成 JSON 和按来源汇总的中文报告。独立写入阶段使用受限角色，校验项目、账号、RLS 和授权，在同一事务执行增量写入、重放、复核历史岗位 ID、链接及首次发现时间。缺少可靠证据的来源暂停入库，来源波动不会批量修改历史状态。已知截止日期继续由网页用于显示「已截止」。

正常批次最多新增 60 条，更新上限为基线的三分之一向下取整（最少 3 条、最多 12 条），最多将 3 条已有岗位标记失效。批次超过上限会停止。若首次导入主要是在补历史证据，可在审核后显式勾选 `insert_only`，只处理新增并报告暂缓的更新数量。历史证据如需集中补录，应另行核对来源和变更。

### 先验收测试库

1. 获批同步、提交和推送后，在测试项目先导出 `jobs` 的 ID、链接和首次发现时间，记录私人表基线。执行 **Actions → JobRadar staging sync acceptance → Run workflow**，先保持选项未勾选。查看 `staging-acceptance`，确认四张私人表的读、增、改、删均被拒绝，以及模拟岗位和异常事务成功回滚。
2. 再次执行并勾选 `real_collection`、`verify_production_path`，先保持 `insert_only` 未勾选。流程使用已有的 `jobradar-staging` 环境和采集账号，调用同一个写入器，复核报告重放后新增和更新均为 0，再回滚整批并比较公共岗位全表。若触发大量更新保护，审核报告后可勾选 `insert_only` 复核首次导入路径。报告产物为 `staging-collection` 和 `staging-write`。核对私人表基线及 A、B 的隔离。

### 正式环境只在批准后配置

#### 上线前的单岗位回滚验收

只在项目 **jobradar-staging**（Ref `rqxklouevsxjmgqdhkdv`）执行。试验期间暂停其他测试写入。用 SQL Editor 在试验前后分别运行同一条只读查询，并在本地比较结果。摘要涵盖全部字段和所有用户，结果只显示每张表的行数及摘要。只需记录是否一致，保留原始摘要在本地。

```sql
select 'jobs' as table_name, count(*) as row_count,
       encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]')::text, 'UTF8')), 'hex') as digest
from public.jobs t
union all
select 'profiles', count(*), encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]')::text, 'UTF8')), 'hex')
from public.profiles t
union all
select 'job_activity', count(*), encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]')::text, 'UTF8')), 'hex')
from public.job_activity t
union all
select 'interviews', count(*), encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]')::text, 'UTF8')), 'hex')
from public.interviews t
union all
select 'manual_jobs', count(*), encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)), '[]')::text, 'UTF8')), 'hex')
from public.manual_jobs t
order by table_name;
```

在 **Actions → JobRadar staging sync acceptance → Run workflow** 选择包含本次改动的默认分支，只勾选 `verify_writer_fixture`。检查 `staging-acceptance` 产物。正常结果为首次新增 1、更新 1，分别重放后新增和更新均为 0；对模拟行修改 ID、名称、链接和首次发现时间以及删除的请求均被拒绝。正常退出和故意触发数据库错误后，原岗位全表与试前逐字段一致。两次事务都只写入同一条随机标记的模拟岗位，保留真实采集的来源校验和批量保护。采集角色的私人表权限拒绝由脚本验证，私人内容是否一致以维护者的前后摘要为准。

#### 配置和手动运行

1. 确认已有可读取的备份，检查正式项目 Ref 为 `hnkwyuhkqzrlhjhiejof`。由维护者审核并在正式项目 SQL Editor 执行 `supabase/007_jobradar_production_collector.review.sql` 一次。创建的 `jobradar_prod_collector` 初始禁止登录，SQL 没有写入业务数据。权限检查发现私人授权、缺少 RLS 或可执行的公开提权函数时会回滚整个迁移。
2. 在可信的本机 `psql` 管理会话确认正式项目，执行 `\password jobradar_prod_collector`，隐藏输入新的独立随机密码。密码生效后执行 `ALTER ROLE jobradar_prod_collector LOGIN;`。使用正式项目 Connect 面板的 Direct 或 Session pooler（5432），下载该项目的 CA 证书。Session pooler 用户名为 `jobradar_prod_collector.hnkwyuhkqzrlhjhiejof`，Direct 用户名为 `jobradar_prod_collector`。DSN 密码如包含保留字符须进行 URL 编码。
3. 在 GitHub **Settings → Environments → New environment** 创建 `jobradar-production`，限制为默认分支。若可配置，设置人工审批人。添加环境 Secrets `JOBRADAR_PROD_JOBS_DSN` 和 `JOBRADAR_PROD_JOBS_CA_PEM`，分别填生产专用采集连接串和 CA PEM 内容。在 Variables 核对 `JOBRADAR_PRODUCTION_PROJECT_REF` 为正式项目 Ref。采集角色与用户名、项目 Ref、会话角色和证书均须匹配。
4. 在 **Actions → JobRadar production jobs** 先手动运行默认选项。检查公开来源和异常。测试库验收与生产审核都通过后，再手动勾选 `apply_production`。检查 `production-sync` 的实际新增、更新、重复和待核验，核对原岗位和个人记录。首次写入若获批采用只新增模式，同时勾选 `insert_only`。
5. 等多次手动写入和报告重放通过后再审批定时运行。UTC 01:17 对应北京时间 09:17。定时触发目前为注释，定时写入还要求仓库变量 `JOBRADAR_ENABLE_SCHEDULED_WRITE=true`。需要紧急暂停时禁用此工作流，并可由管理员执行 `ALTER ROLE jobradar_prod_collector NOLOGIN;` 停止新的连接。

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


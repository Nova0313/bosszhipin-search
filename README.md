# BOSS 直聘岗位检索工具 v1.4.1

纯 Python/CDP 实现的 BOSS 岗位检索工具。支持按关键词或公司名检索，可选用 LLM
语义理解自然语言需求和岗位内容，判断岗位是否相关。公司聚合、页面抓取、工商字段提取、
法定主体去重和 CSV 输出全部由代码完成。

## 共同准备

Python 需要 3.9 或更高版本。

```bash
cd bosszhipin-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

首次使用时，启动项目专用 Chrome：

```bash
python scripts/boss_cdp_raw.py --setup-chrome
```

在打开的 Chrome 中登录 BOSS 直聘，后续检索时保持该 Chrome 运行。登录状态保存在独立目录，不会使用主 Chrome 的 Cookie。
需要更换登录账号时，可在 Web 控制台右上角点击“切换账号”，或运行：

```bash
python scripts/boss_cdp_raw.py --switch-account
```

该操作只会清理 BOSS 专用 Chrome profile 中的 BOSS 登录状态，不影响主 Chrome。检索任务运行期间不允许切换账号。

## 使用路线一：HTML GUI

在项目根目录运行：

```bash
python -m scripts.web_app
```

页面会自动打开。如果没有打开，请访问：

```text
http://127.0.0.1:8765/
```

在页面中：

1. 选择“按关键词”或“按公司”。
2. 填写规则 CSV/XLSX 的本机路径。
3. “高级选项”中的 LLM 岗位相关性筛选默认开启，可展开配置岗位需求；发布时间区间和 JD 详情数量上限会在启用对应开关后展开。
4. 点击“开始检索”，页面会显示进度和日志。
5. 任务失败或手动停止后，点击“继续中断任务”，会沿用原结果目录并从已保存的列表、发布时间或 JD 详情检查点继续。

不想自动打开浏览器时：

```bash
python -m scripts.web_app --no-browser
```

## 使用路线二：纯 Python 命令行

### 直接检索一个关键词

```bash
python scripts/boss_cdp_raw.py \
  --keyword "Python 后端" \
  --city 上海 \
  --pages 3
```

不抓取 JD 详情：

```bash
python scripts/boss_cdp_raw.py \
  --keyword "AI Agent" \
  --city 北京 \
  --pages 2 \
  --no-detail
```

### 根据表格批量检索

按关键词：

```bash
python -m scripts.batch_search examples/keyword_rules.csv \
  --output-dir result
```

按公司名：

```bash
python -m scripts.batch_search examples/company_rules.csv \
  --mode company \
  --output-dir result
```

只检查表格会生成哪些搜索组合，不连接 Chrome：

```bash
python -m scripts.batch_search examples/keyword_rules.csv --dry-run
```

常用参数：

- `--pages 5`：每个搜索组合最多抓取 5 页；不填则抓取所有页。
- `--interval 3`：列表翻页、岗位详情和公司页的请求间隔秒数；默认 3 秒，组合间固定等待 10 秒。
- `--keyword-match-threshold 0.8`：岗位关键词模糊匹配阈值，范围 0-1；值越高越严格。
- `--fetch-detail`：抓取 JD（默认不抓取）。
- `--no-detail`：不抓取 JD。
- `--max-details 100`：最多抓取 100 个 JD。
- `--published-from 2026-08-01`：仅保留该日期及之后发布/更新的岗位。
- `--published-to 2026-08-31`：仅保留该日期及之前发布/更新的岗位。
- `--fetch-publish-time`：逐个访问岗位详情页，补查发布/更新时间。
- `--no-fetch-publish-time`：不额外补查，仅使用列表接口自带的时间。
- `--output-fields job_id,title,company,location,salary,experience,publish_date,jd`：指定输出字段。
- `--job-requirements "需要机器人视觉、VLA 或多模态算法岗，排除销售"`：启用 LLM 岗位语义判断。
- `--llm-batch-size 20`：每次发给 LLM 的岗位数。

发布时间区间的起止日期均包含当天。命令行未显式指定上述开关时，
填写日期区间会自动启用补查。补查时，程序会参考
`boss_show_time` 的识别方式，优先使用列表已有时间，否则逐个读取详情页中的
`datePosted`、`publishTime`、发布时间或更新时间文本。无法识别可靠时间的岗位会被
排除并计入日志；该步骤会增加详情页请求次数。候选岗位列表和发布时间进度会保存到
输出根目录的 `.checkpoints` 中；任务中断后，使用相同规则表和检索条件重新运行即可
自动继续。完整成功后会删除检查点。单次任务的全局页面/API 请求上限为 5000 次。

## LLM 岗位语义判断与公司工商信息

LLM 仅用于理解用户需求和岗位上下文，作出语义相关性判断；不做关键词包含、固定权重或预设分数阈值筛选。
填写岗位需求不会改变原检索流程：代码仍会先执行职位标题模糊匹配，并在连续 5 个岗位未匹配时切换到下一条规则。
是否抓取 JD 完全由用户的“抓取岗位 JD 详情”配置决定。完成检索和可选 JD 抓取后，才将整张岗位结果表交给 LLM 判断。
如果 API Key 来自硅基流动，启动 Web 服务或运行批量命令前设置：

```bash
export LLM_PROVIDER="siliconflow"
export SILICONFLOW_API_KEY="your-siliconflow-api-key"
export SILICONFLOW_MODEL="deepseek-ai/DeepSeek-V4-Flash"
# 可选，默认 https://api.siliconflow.cn/v1
export SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
# 可选，默认 120 秒
export LLM_TIMEOUT_SECONDS="120"
```

模型 ID 必须使用硅基流动模型广场展示的完整 ID，不能使用 DeepSeek 官方模型 ID。
只设置了 `SILICONFLOW_API_KEY` 时，可以省略 `LLM_PROVIDER=siliconflow`。

使用 DeepSeek 官方 API 时则设置：

```bash
export LLM_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="your-deepseek-api-key"
export DEEPSEEK_MODEL="your-deepseek-model-id"
# 可选，默认 https://api.deepseek.com
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
# 可选，默认 120 秒
export LLM_TIMEOUT_SECONDS="120"
```

如果只设置了 `DEEPSEEK_API_KEY`，可以省略 `LLM_PROVIDER=deepseek`，程序会自动选择 DeepSeek。
DeepSeek 调用走原生 Responses API，并要求模型按岗位判断协议返回结构化结果。

使用 Gemini Developer API 时则设置：

```bash
export LLM_PROVIDER="gemini"
export GEMINI_API_KEY="your-gemini-api-key"
export GEMINI_MODEL="your-gemini-model-id"
# 可选，默认 https://generativelanguage.googleapis.com/v1beta
export GEMINI_BASE_URL="https://generativelanguage.googleapis.com/v1beta"
# 可选，默认 120 秒
export LLM_TIMEOUT_SECONDS="120"
```

如果已设置 `GEMINI_API_KEY`，可以省略 `LLM_PROVIDER=gemini`，程序会自动选择 Gemini。
Gemini 调用走原生 `models/{model}:generateContent` 接口。项目仍保留
`LLM_PROVIDER=openai` 的 OpenAI Responses API 兼容能力。若同时配置多个服务商的
API Key，请显式设置 `LLM_PROVIDER`，避免选择错误。

命令行示例：

```bash
python -m scripts.batch_search examples/keyword_rules.csv \
  --job-requirements "我们需要机器人视觉、VLA 或多模态算法岗，排除纯销售和测试岗" \
  --output-dir result
```

处理顺序为：代码检索→代码标题模糊匹配和连续 5 个未匹配停搜→按用户配置决定是否抓取 JD→整张岗位表交给 LLM 语义判断→代码聚合公司→代码抓取工商信息、去重并输出 CSV。
开启后，岗位需求和候选岗位字段会发送给所配置的 LLM。
公司全称只从“企业名称/公司全称”标签提取，不用品牌简称猜测；页面未公开或无法识别时，
`companies.csv` 中对应值留空；全称和代码都无法识别的公司仅保留在
`metadata.json` 中供排查。API 密钥仅从环境变量读取，不写入 Web 命令、日志或结果文件。

## 检索规则

关键词模式表头：

```text
搜索关键词,城市,薪资待遇,工作经验,求职类型
```

公司模式表头：

```text
公司名称,城市,薪资待遇,工作经验,求职类型
```

- 同一列中的值是 OR。
- 关键词/公司、城市、薪资、经验、求职类型各列之间是 AND。
- 求职类型列可选，支持“不限”、“全职”和“兼职”；省略该列时默认不限。
- 各列长度可以不同，行之间没有对应关系。
- 默认最多生成 64 个搜索组合。
- 每页岗位会按返回顺序再做一次代码匹配：关键词模式对职位名称先做包含匹配，未命中时再做模糊匹配（不限关键词长度，多个空白分隔词均须匹配）；公司模式匹配公司名。
- 连续 5 个岗位都未匹配当前检索词，会结束当前搜索组合。该逻辑不受 LLM 岗位需求配置影响。

可直接参考 [关键词规则](examples/keyword_rules.csv) 和 [公司规则](examples/company_rules.csv)。

## 输出文件

批量检索会在输出目录中创建一个独立任务文件夹，包含：

- `jobs.csv`
- `jobs.json`
- `metadata.json`
- `companies.csv`（填写岗位需求、启用 LLM 语义判断时生成）

默认输出字段为 `job_id`、`title`、`location`、`salary`、`experience`、
`company_scale`、`company_stage` 和 `company_industry`。

`companies.csv` 固定只包含 `company_full_name` 和
`unified_social_credit_code` 两列。

## 常见问题

检查环境和 Chrome 连接：

```bash
python scripts/boss_cdp_raw.py --check
```

如果项目无法自动找到 Chrome：

```bash
python scripts/boss_cdp_raw.py --setup-chrome \
  --chrome-path "/Chrome 可执行文件路径"
```

## 合规说明

仅用于个人求职研究和技术学习。请遵守 BOSS 直聘用户协议及所在地法律法规；遇到验证或风控页面时，程序会停止。

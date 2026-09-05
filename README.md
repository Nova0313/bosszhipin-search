# BOSS 直聘岗位检索工具 v1.4.2

一个基于 Python 和 Chrome DevTools Protocol（CDP）的 BOSS 直聘岗位检索工具。
它支持按关键词或公司批量组合检索、职位去重、发布时间筛选、JD 详情抓取、
LLM 岗位语义判断、公司工商信息整理和中断任务续跑。

> 项目依赖已登录的 BOSS 页面和站点当前的页面/API 结构。请仅用于个人求职研究和技术学习，
> 并遵守 BOSS 直聘用户协议及所在地法律法规。

## 功能概览

- 本地 HTML 控制台：可配置规则表、检索模式、页数、时间区间、JD、LLM 和输出字段。
- 表格批量检索：支持 CSV、TSV、XLSX 和 XLSM，自动展开关键词/公司、城市、薪资、经验和求职类型组合。
- 两种检索模式：按职位关键词检索，或按公司名称检索并校验返回公司。
- 本地二次匹配：关键词模式使用包含+模糊匹配，公司模式支持包含或严格匹配。
- 发布时间：可逐岗位补查发布/更新时间，再按起止日期筛选。
- JD 详情：按需抓取 JD、技能标签、福利和招聘者活跃状态。
- LLM 语义判断：支持硅基流动、DeepSeek、Gemini 和 OpenAI，并保留每个命中岗位的判断理由。
- 公司聚合：将 LLM 命中岗位按公司聚合，读取企业全称和统一社会信用代码，再按法定主体去重。
- 岗位-公司对应表：保留岗位 ID、BOSS 公司 ID（`encrypt_brand_id`）、工商全称和统一社会信用代码的逐岗位关联结果。
- 可恢复任务：列表组合、发布时间和 JD 详情都有检查点保护。
- 请求频率保护：操作间隔基于设定值随机化，并有组合数、请求数、连续失配和风控停止机制。

## 环境要求

- Python 3.9 或更高版本。
- Google Chrome、Chromium、Microsoft Edge 或 Brave。Safari 不支持本项目使用的 CDP。
- 可正常登录 BOSS 直聘的账号。
- 只有启用 LLM 语义判断时才需要对应服务商的 API Key 和模型 ID。

## 安装

在项目根目录创建虚拟环境并安装依赖：

```bash
cd bosszhipin-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell 的虚拟环境激活命令为：

```powershell
.venv\Scripts\Activate.ps1
```

## 首次使用：启动专用 Chrome

启动项目专用的 Chromium 浏览器：

```bash
python scripts/boss_cdp_raw.py --setup-chrome
```

程序会使用独立 profile，默认位于：

```text
~/.boss-zhipin-python/chrome-profile
```

在打开的浏览器中登录 BOSS 直聘。后续检索期间需要保持该浏览器运行。
专用 profile 与主 Chrome 隔离，默认不会复制主 Chrome 的 Cookie。

如果无法自动找到浏览器：

```bash
python scripts/boss_cdp_raw.py --setup-chrome \
  --chrome-path "/path/to/chrome"
```

其他常用管理命令：

```bash
# 检查依赖、CDP 和 BOSS 登录状态
python scripts/boss_cdp_raw.py --check

# 用真实 Chrome/CDP 执行一次搜索 API 冒烟测试，不写入结果
python scripts/boss_cdp_raw.py --smoke-test

# 更换专用 Chrome 中的 BOSS 账号
python scripts/boss_cdp_raw.py --switch-account

# 关闭专用 Chrome，不影响主 Chrome
python scripts/boss_cdp_raw.py --stop-chrome
```

`--switch-account` 会清理专用 profile 中的 BOSS 登录状态并打开登录页，不影响主 Chrome。
检索任务运行期间不能切换账号。

## 推荐用法：Web 控制台

在项目根目录运行：

```bash
python -m scripts.web_app
```

默认会打开 `http://127.0.0.1:8765/`。不想自动打开浏览器时：

```bash
python -m scripts.web_app --no-browser
```

也可指定监听地址和端口：

```bash
python -m scripts.web_app --host 127.0.0.1 --port 9000
```

### Web 默认配置

| 配置 | 默认值 |
| --- | --- |
| 检索模式 | 按关键词 |
| 每个组合页数 | 全部页，直到无更多数据、空页或没有新岗位 |
| 抓取基准间隔 | 3 秒，每次在 2.4–3.6 秒内随机 |
| 关键词模糊匹配阈值 | 0.8 |
| 查询发布时间 | 关闭 |
| 抓取 JD 详情 | 关闭 |
| 公司名校验 | 简称/全称包含匹配 |
| LLM 岗位相关性筛选 | 开启，使用页面中的默认岗位需求 |
| 输出根目录 | 项目下的 `result/` |

开始前至少需要选择检索模式并填写规则表路径。高级选项中可以：

- 开关 LLM 筛选并修改自然语言岗位需求。
- 设置每个搜索组合的页数上限。
- 启用发布时间查询和日期区间筛选。
- 启用 JD 抓取并限制最多抓取数量。
- 选择最终 CSV/JSON 的输出字段。
- 公司模式下选择包含匹配或严格匹配。

同一时间 Web 控制台只允许一个检索任务运行。任务失败或手动停止后，
可在服务未重启的前提下点击“继续中断任务”，复用原命令和结果目录。

## 检索规则表

### 支持的文件

- `.csv`
- `.tsv`
- `.xlsx`
- `.xlsm`

不支持旧版 `.xls`；请先另存为 `.xlsx` 或 `.csv`。Excel 默认读取第一个工作表，
命令行可通过 `--sheet` 指定工作表名。

### 表头

关键词模式：

```csv
搜索关键词,城市,薪资待遇,工作经验,求职类型
机器人,全国,不限,不限,全职
VLA,,,,
```

公司模式：

```csv
公司名称,城市,薪资待遇,工作经验,求职类型
字节跳动,北京,20-50K,3-5年,全职
腾讯,,,,
```

必需列：

- 关键词模式：搜索关键词、城市、薪资待遇、工作经验。
- 公司模式：公司名称、城市、薪资待遇、工作经验。
- 求职类型为可选列；省略或整列留空时按“不限”处理。

表头同时支持英文别名，例如 `keyword`、`company`、`city`、`salary`、
`experience` 和 `job_type`。

### 组合逻辑

规则表不按行建立绑定关系，而是将每一列分别汇总为 OR 候选集，再在列之间做 AND 笛卡尔积。

```text
搜索组合数 = 关键词/公司数 × 城市数 × 薪资数 × 经验数 × 求职类型数
```

- 同一单元格可使用逗号、中文逗号、分号、`|`、顿号或换行填写多个 OR 值。
- 同一列的重复值会去重。
- 薪资、经验和求职类型留空时按“不限”处理。
- 搜索关键词/公司名和城市列不能全空；全国检索请在城市列填“全国”。
- 默认最多展开 64 个组合，可用 `--max-combinations` 显式调整。

常用友好标签包括：

- 薪资：`不限`、`10K以上`、`20K以上`、`50K以上`，以及 BOSS 支持的标准薪资段。
- 经验：`不限`、`无经验`、`3年以上`、`5年以上`、`10年以上`，以及 BOSS 支持的标准经验段。
- 求职类型：`不限`、`全职`、`兼职`。

可参考 [机器人关键词规则](examples/robotics_keyword_rules.csv) 和
[公司规则](examples/company_rules.csv)。在真正连接 Chrome 前，建议先用 `--dry-run` 检查展开后的组合。

## 批量命令行

### 基本示例

按关键词检索：

```bash
python -m scripts.batch_search examples/robotics_keyword_rules.csv \
  --output-dir result
```

按公司检索：

```bash
python -m scripts.batch_search examples/company_rules.csv \
  --mode company \
  --output-dir result
```

只校验规则并查看展开后的组合，不启动 Chrome 职位抓取：

```bash
python -m scripts.batch_search examples/robotics_keyword_rules.csv --dry-run
```

一个较完整的示例：

```bash
python -m scripts.batch_search examples/robotics_keyword_rules.csv \
  --pages 5 \
  --interval 3 \
  --keyword-match-threshold 0.8 \
  --fetch-publish-time \
  --published-from 2026-08-01 \
  --published-to 2026-08-31 \
  --fetch-detail \
  --max-details 100 \
  --output-fields job_id,title,company,location,salary,experience,publish_date,jd,job_link \
  --output-dir result
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--mode keyword\|company` | 检索模式，默认 `keyword` |
| `--sheet NAME` | Excel 工作表名，默认第一个 |
| `--pages N` | 每个组合的页数上限；不填时抓取全部页 |
| `--max-combinations N` | 规则表最大展开组合数，默认 64 |
| `--interval SECONDS` | 列表翻页、发布时间、JD 和公司页的基准间隔，默认 3 秒 |
| `--keyword-match-threshold N` | 关键词模糊匹配阈值，范围 0–1，默认 0.8 |
| `--company-match contains\|exact` | 公司名校验策略，默认 `contains` |
| `--fetch-publish-time` | 逐岗位打开详情页补查发布/更新时间 |
| `--no-fetch-publish-time` | 不额外补查，只使用列表已有时间 |
| `--published-from YYYY-MM-DD` | 发布时间起始日期，包含当天 |
| `--published-to YYYY-MM-DD` | 发布时间结束日期，包含当天 |
| `--fetch-detail` / `--no-detail` | 启用/关闭 JD 抓取；批量 CLI 默认关闭 |
| `--max-details N` | 最多抓取 N 个 JD；未抓取岗位仍保留，JD 为空 |
| `--output-fields A,B,...` | 指定 `jobs.csv` 和 `jobs.json` 中的字段 |
| `--job-requirements TEXT` | 启用 LLM 岗位语义判断，仅保留相关岗位 |
| `--llm-batch-size N` | LLM 每批岗位数，默认 20，范围 1–100 |
| `--allow-dom-fallback` | API 无数据时允许从 DOM 降级提取；默认关闭 |
| `--analysis` | 在命令行输出固定规则聚合分析 |
| `--dry-run` | 校验表格并输出检索计划，不抓取岗位 |
| `--output-dir DIR` | 结果根目录；默认为项目下的 `result/` |
| `--cdp-port PORT` | CDP 调试端口，默认 9222 |

可用的输出字段：

```text
job_id,title,company,location,salary,experience,publish_date,jd,job_link,
boss_active_status,company_scale,company_stage,company_industry,skills,welfare,
llm_match_reason
```

默认输出：

```text
job_id,title,location,salary,experience,company_scale,company_stage,company_industry
```

## 随机操作间隔

`--interval` 和 Web 界面的“抓取基准间隔”设置的是基准值 `t`，不是固定等待时间。
列表翻页、岗位发布时间读取、JD 详情抓取和公司页读取前，都会独立生成实际间隔：

```text
实际间隔 = random(0.8 × t, 1.2 × t)
```

| 基准间隔 | 每次的实际间隔 |
| --- | --- |
| 3 秒（默认） | 2.4–3.6 秒 |
| 5 秒 | 4–6 秒 |
| 0 秒 | 0 秒（关闭所有操作和组合间等待） |

当 `t > 0` 时，搜索组合切换的间隔不跟随 `t` 的大小，而是以 10 秒为基准，
每次在 8–12 秒之间随机等待。将 `--interval` 设为 `0` 会同时关闭这一组合间等待。

## 发布时间和日期筛选

- 只使用 `--fetch-publish-time` 时，程序会补查时间但不按日期删除岗位。
- 填写 `--published-from` 或 `--published-to` 会默认自动启用时间补查。
- 显式使用 `--no-fetch-publish-time` 时，日期筛选只使用列表已有的时间。
- 日期起止边界都包含当天。
- 无法识别可靠时间的岗位不会被猜测；启用日期区间时，这些岗位会被排除并计入日志和元数据。

程序会优先使用列表已有时间，否则读取详情页中的 `datePosted`、`publishTime`、
“发布时间”或“更新时间”等明确信息。

## LLM 岗位语义判断

LLM 只用于理解自然语言岗位需求和岗位上下文，判断每个岗位是否相关。
它不替代本地职位标题/公司名校验，也不负责搜索、公司聚合、工商字段提取或法定主体去重。

批量处理顺序：

```text
检索组合
  → 本地标题/公司名匹配
  → 可选的发布时间筛选
  → 可选的 JD 抓取
  → LLM 语义判断
  → 公司聚合、工商信息抓取和去重
  → CSV/JSON 输出
```

批量 CLI 只有在 `--job-requirements` 非空时才启用 LLM。Web 控制台默认开启 LLM，
可在高级选项中关闭。启用后，岗位需求和候选岗位字段会发送给选定的 LLM 服务商。

### 配置硅基流动

```bash
export LLM_PROVIDER="siliconflow"
export SILICONFLOW_API_KEY="your-siliconflow-api-key"
export SILICONFLOW_MODEL="provider/model-id"
# 可选，默认 https://api.siliconflow.cn/v1
export SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
```

### 配置 DeepSeek

```bash
export LLM_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="your-deepseek-api-key"
export DEEPSEEK_MODEL="your-deepseek-model-id"
# 可选，默认 https://api.deepseek.com
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

### 配置 Gemini

```bash
export LLM_PROVIDER="gemini"
export GEMINI_API_KEY="your-gemini-api-key"
export GEMINI_MODEL="your-gemini-model-id"
# 可选，默认 https://generativelanguage.googleapis.com/v1beta
export GEMINI_BASE_URL="https://generativelanguage.googleapis.com/v1beta"
```

### 配置 OpenAI

```bash
export LLM_PROVIDER="openai"
export OPENAI_API_KEY="your-openai-api-key"
export OPENAI_MODEL="your-openai-model-id"
# 可选，默认 https://api.openai.com/v1
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

所有服务商都可用下列环境变量修改请求超时，默认 120 秒：

```bash
export LLM_TIMEOUT_SECONDS="120"
```

如果不设置 `LLM_PROVIDER`，程序会按硅基流动、DeepSeek、Gemini、OpenAI 的顺序根据已存在的
API Key 选择服务商。同时配置多家时建议显式设置 `LLM_PROVIDER`。

命令行示例：

```bash
python -m scripts.batch_search examples/robotics_keyword_rules.csv \
  --job-requirements "需要机器人视觉、VLA 或多模态算法岗，排除纯销售和测试岗" \
  --output-fields job_id,title,company,location,salary,llm_match_reason \
  --output-dir result
```

API 密钥只从环境变量读取，不会写入 Web 启动命令、日志或结果文件。

## 公司工商信息

启用 LLM 语义判断后，程序会将命中岗位聚合为公司，并访问 BOSS 公司页提取：

- `company_full_name`：企业全称。
- `unified_social_credit_code`：统一社会信用代码。

公司全称只从页面中“企业名称”或“公司全称”等明确标签提取，不使用品牌简称猜测。
页面未公开或无法识别时字段留空；全称和代码都为空的公司不写入 `companies.csv`，
但会保留在 `metadata.json` 中供排查。

## 输出和检查点

每次批量任务会在输出根目录下创建独立目录：

```text
result/
├── .checkpoints/   # 任务中断期间保留，完成后自动清理
└── robotics_keyword_rules_keyword_20260904_123456_123456/
    ├── jobs.csv
    ├── jobs.json
    ├── metadata.json
    ├── companies.csv   # 仅启用 LLM 岗位判断时生成
    └── job_company_mapping.csv  # 岗位与 BOSS 公司及工商主体对应表
```

- `jobs.csv`：使用 UTF-8 BOM，便于 Excel 直接打开；字段由 `--output-fields` 控制。
- `jobs.json`：与 CSV 使用相同的字段投影。
- `metadata.json`：保留检索计划、组合运行统计、选项、日期筛选统计、LLM 信息和公司数据。
- `companies.csv`：只包含 `company_full_name` 和 `unified_social_credit_code` 两列。
- `job_company_mapping.csv`：启用 LLM 岗位判断时生成，每个最终岗位一行，包含
  `job_id`、`boss_company_id`、`company_full_name` 和 `unified_social_credit_code`。
  其中 `boss_company_id` 来自 BOSS 接口返回的 `encrypt_brand_id`；未抓到工商信息时仍保留岗位和公司 ID，工商字段为空。

BOSS 直聘职位接口中的公司标识通常是加密的 `encryptBrandId`，不是公开的数字型公司 ID。
抓取时会将其保存为 `encrypt_brand_id`，并据此生成公司主页链接；如果只使用已经导出的、未包含该字段的岗位 CSV，无法事后可靠恢复 BOSS 公司 ID。

列表检索和发布时间进度保存在输出根目录的 `.checkpoints/` 中。重新使用相同规则表内容和
会影响候选岗位的检索参数执行时，程序会自动识别并继续。Web 控制台的“继续中断任务”
还会复用原任务目录，将其中的 `jobs.json` 作为 JD 部分结果，跳过已抓取岗位。
任务完整成功后，对应列表/发布时间检查点会被删除。

## 安全限制与停止条件

- 默认最多展开 64 个搜索组合。
- 单次任务全局页面/API 请求上限为 5000 次，列表、发布时间、JD 和公司页共享该配额。
- 按全部页抓取时，遇到无更多数据、空页或没有新岗位会自动停止当前组合。
- 连续 5 个岗位未通过当前关键词/公司名本地校验时，会提前结束当前搜索组合。
- 检测到未登录、验证码、访问频繁、安全校验或环境异常时会停止，不会持续重试。
- API 无数据时默认不使用 DOM 降级提取，避免加密字体造成不可靠薪资。
- Web 控制台同一时间只运行一个任务。
- 网络请求、Chrome 启动和登录等待都有超时和有限重试。

## 单关键词原始 CLI

`scripts/boss_cdp_raw.py` 适合一次性检索、调试、合并旧数据或生成本地分析。
它与批量 CLI 有两个重要差异：默认抓取 3 页，并且默认抓取 JD 详情。

```bash
# 搜索 3 页并抓取 JD（原始 CLI 的默认行为）
python scripts/boss_cdp_raw.py \
  --keyword "Python 后端" \
  --city 上海 \
  --pages 3

# 只抓列表
python scripts/boss_cdp_raw.py \
  --keyword "AI Agent" \
  --city 北京 \
  --pages 2 \
  --no-detail

# 使用 BOSS 筛选代码
python scripts/boss_cdp_raw.py \
  --keyword "Java 风控" \
  --city 上海 \
  --scale 305 \
  --salary 406 \
  --experience 105

# 只分析已有列表文件
python scripts/boss_cdp_raw.py \
  --input ~/.boss-zhipin-python/job-result/boss_jobs_YYYYMMDD_HHMMSS.json \
  --analysis \
  --no-detail
```

常用工具参数：

- `--list-cities [关键词]`：查看城市和代码，可按关键词过滤。
- `--format json|csv`：输出 JSON，或同时写入 CSV。
- `--output PATH` / `--detail-output PATH`：指定列表和详情输出路径。
- `--merge PATH`：按 `job_id` 合并旧 JSON 数据。
- `--analysis`：输出固定规则分析报告。
- `--close-chrome`：抓取正常结束后自动关闭专用 Chrome；异常退出时保留浏览器和登录态。

未显式设置输出路径时，原始 CLI 的默认结果目录为：

```text
~/.boss-zhipin-python/job-result
```

查看全部原始 CLI 参数：

```bash
python scripts/boss_cdp_raw.py --help
```

## 常见问题

### 提示未登录或请先登录

确认是在项目专用 Chrome 中登录，而不是主 Chrome。可运行：

```bash
python scripts/boss_cdp_raw.py --check
```

### 遇到访问频繁、验证码或安全校验

程序会停止并保留检查点。请先在专用 Chrome 中完成验证或等待限制解除，
不要连续重启任务。恢复后使用原配置继续。

### 规则表提示组合数过多

先运行 `--dry-run` 查看哪些列产生了过多组合。优先拆分规则表；
确认需要更大范围时，再使用 `--max-combinations`。

### 输出中 JD 为空

批量 CLI 和 Web 控制台默认不抓取 JD。请启用 `--fetch-detail` 或 Web 中的“抓取岗位 JD 详情”。
如果设置了 `--max-details`，超出上限的岗位仍会输出，但 JD 为空。

### 页面 API 没有数据

默认不使用 DOM 降级提取，因为薪资可能受加密字体影响。
只有明确接受这一数据质量风险时，才建议使用 `--allow-dom-fallback`。

### 规则或检索参数变更后没有续跑

检查点会绑定规则表内容和会影响候选岗位的参数。修改这些条件后，
程序会将其视为新任务，避免把旧候选结果混入新检索。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## License

[MIT License](LICENSE)

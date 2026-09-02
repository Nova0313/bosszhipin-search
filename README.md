# BOSS 直聘岗位检索工具

纯 Python 实现，不使用大语言模型。支持按关键词或公司名检索岗位，输出 CSV 和 JSON。

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
3. 按需调整页数、请求间隔、JD 和输出字段。
4. 点击“开始检索”，页面会显示进度和日志。

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
- `--interval 8`：请求间隔秒数。
- `--no-detail`：不抓取 JD。
- `--max-details 100`：最多抓取 100 个 JD。
- `--output-fields job_id,title,company,location,salary,experience,jd`：指定输出字段。

## 检索规则

关键词模式表头：

```text
搜索关键词,城市,薪资待遇,工作经验
```

公司模式表头：

```text
公司名称,城市,薪资待遇,工作经验
```

- 同一列中的值是 OR。
- 关键词/公司、城市、薪资、经验各列之间是 AND。
- 各列长度可以不同，行之间没有对应关系。
- 默认最多生成 64 个搜索组合。

可直接参考 [关键词规则](examples/keyword_rules.csv) 和 [公司规则](examples/company_rules.csv)。

## 输出文件

批量检索会在输出目录中创建一个独立任务文件夹，包含：

- `jobs.csv`
- `jobs.json`
- `metadata.json`

默认输出字段为 `job_id`、`title`、`company`、`location`、`salary`、`experience` 和 `jd`。

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

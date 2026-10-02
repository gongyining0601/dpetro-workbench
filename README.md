# 锦州石化投稿辅助工具（DPetroWorkbench）

零成本云端投稿辅助软件——媒体库自动采集 + 5 分钟审核 + 常规选题日历 + 选题语义对标 + 成稿体检 + 用稿规律学习。

服务对象：锦州石化企业报图文记者，给《中国石油报》《辽宁日报》投稿。

## 架构（2026-09-29 上云版）

- **应用层**：Streamlit Community Cloud（免费，1GB RAM）
- **数据层**：Supabase PostgreSQL（免费 500MB）
- **嵌入层**：Silicon Flow 免费 API（BAAI/bge-large-zh-v1.5）
- **调度层**：GitHub Actions（每日北京 07:00 自动爬取）

全部本地依赖（torch/bge）已剥离，云端启动快、内存占用低（~300MB / 1GB 上限）。

## 功能说明

软件共 **5 个功能模块**，通过顶部 Tab 切换。

### 1. 今日审核

自动爬取《中国石油报》《辽宁日报》的最新稿件，AI 预过滤后只保留**石油石化行业相关**的稿件供你审核。

**操作：**
- 浏览稿件标题、来源、栏目、正文摘要
- 点击「相关」→ 稿件进入「历史已审」，可作为选题参考
- 点击「借鉴」→ 稿件进入「历史已审」并标记为可借鉴
- 点击「无关」→ **直接删除该稿件**（不保留记录）

### 2. 历史已审

展示所有标记为「相关」或「借鉴」的稿件，按审核时间倒序排列，方便随时回看参考。

### 3. 常规日历

- **常规选题提醒**：根据年初选题计划（春检、安全月、七一、冬季保供、VOCs治理等），自动提示未来 30 天内需要关注的选题
- **投稿记录管理**：记录每篇投稿的选题、目标媒体、目标栏目、投稿日期、结果（录用/退稿/待审）
- **命中率统计**：按栏目统计投稿命中率，辅助判断哪个栏目更容易中稿

### 4. 选题对标

输入一个选题关键词，系统会：
- 从历史已审稿件中语义检索最相关的 5 篇
- 给出可借鉴的写作角度建议

帮助你在写稿前快速找到同主题的参考范文和切入角度。

### 5. 成稿体检

写完稿件后，粘贴标题和正文，系统会自动检查：
- **模糊时间**：如「最近」「近日」等，需改为具体日期
- **空泛数据**：如「大幅提升」「国内第一」等，需补充具体数字和来源
- **图片说明**：检测图片说明是否规范
- **三版适配**：自动生成《辽宁日报》版、《中国石油报》版、企业内网版三个版本的字数和角度建议

## 使用指南

### 日常工作流

1. **每天打开**「今日审核」，浏览 AI 筛选后的石化相关稿件
2. 对有价值的稿件点「相关」或「借鉴」，无关的点「无关」删除
3. 写稿前到「选题对标」搜索关键词，找参考范文和角度
4. 写完后到「成稿体检」检查问题，按三版适配调整
5. 投稿后到「常规日历」记录投稿信息，跟踪命中率

### 访问方式

| 场景 | 地址 | 说明 |
|------|------|------|
| 云端（推荐） | https://<your-app>.streamlit.app | 手机/任意电脑可访问，无需本机开机 |
| 本机调试 | http://localhost:8501 | 需在本机运行 streamlit run app.py |

### 数据更新

- 爬虫由 GitHub Actions 每日北京 07:00 自动运行
- 也可在 GitHub 仓库 → Actions → Daily Crawl → Run workflow 手动触发

## 部署步骤

### 1. 准备外部服务（一次性）

| 服务 | 注册地址 | 用途 | 免费额度 |
|---|---|---|---|
| GitHub | github.com | 代码仓库 + Actions | 公开仓库免费 |
| Supabase | supabase.com | PostgreSQL 数据库 | 500MB / 2 个项目 |
| Silicon Flow | siliconflow.cn | bge 嵌入 API | 注册送 14 元 |
| Streamlit Cloud | streamlit.io | 跑审核台 app.py | 1GB RAM |

注册后，在 Supabase 拿到 **connection string**（Settings → Database → Connection string → URI），在 Silicon Flow 拿到 **API Key**。

### 2. Fork / 导入仓库到你的 GitHub

把本项目推到 GitHub 公开仓库（项目本身不涉密，公开仓库可免费用 Actions）。

### 3. 配 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret，添加两个：

- `DATABASE_URL` = Supabase connection string
- `SILICONFLOW_API_KEY` = Silicon Flow API Key

### 4. 部署 Streamlit Cloud

streamlit.io → 用 GitHub 登录 → New app → 选你的仓库 → 主文件填 `app.py` → 同样在 Secrets（Streamlit Cloud 自己的 secrets 管理）里加 `DATABASE_URL` 和 `SILICONFLOW_API_KEY`。

部署完成会拿到 `https://<your-app>.streamlit.app` 公网 HTTPS 链接，手机/任意电脑浏览器都能访问。

### 5. 首次跑爬虫填库

GitHub 仓库 → Actions → Daily Crawl → Run workflow 手动跑一次。

跑完去 Streamlit Cloud app 刷新，「今日审核」就能看到当天真实爬取的稿件。

## 本地开发/调试

如果想在本地改代码再推上去：

```powershell
cd <项目目录>
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 复制 .env.example 为 .env，填入 DATABASE_URL 和 SILICONFLOW_API_KEY
copy .env.example .env

# 启动审核台
python -B -m streamlit run app.py

# 单独跑爬虫
python -B crawler.py
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `requirements.txt` | 依赖清单（无 torch，仅 streamlit + psycopg2 + numpy + 爬虫 deps） |
| `config.py` | 媒体源/栏目/常规选题种子 + 环境变量读取（DATABASE_URL / SILICONFLOW_API_KEY） |
| `db.py` | PostgreSQL 建表 + DAO（psycopg2，参数化绑定 `%s`） |
| `crawler.py` | 爬虫（robots.txt 检查 + 节流 + 按 source 分派解析器） |
| `calendar_engine.py` | 常规选题日历 + 投稿命中率统计 |
| `topic_matcher.py` | 选题语义对标（Silicon Flow API 嵌入 + PG 向量表 + numpy cosine） |
| `vector_store.py` | PG 表 `article_embedding` 读写 + 内存 numpy 暴力 cosine |
| `draft_checker.py` | 成稿体检（正则规则）+ 三版适配 |
| `app.py` | Streamlit 审核台主入口，5 个 Tab |
| `.github/workflows/crawl.yml` | GitHub Actions 每日定时爬虫 |
| `.streamlit/config.toml` | Streamlit Cloud 配置 |
| `.env.example` | 环境变量模板（复制为 .env 填值，勿提交） |

## 安全合规

- 严守 robots.txt（`crawler.py` 的 `robots_allows` 已实现）
- 节流 `CRAWL_INTERVAL_SECONDS=5` 秒
- UA 自报家门（`config.USER_AGENT`，请把邮箱改成真实联系邮箱）
- SQL 全用参数化绑定（防注入）
- 时间一律存 ISO 字符串
- 数据库密码 / API Key 全走环境变量 / Secrets，不进代码、不进 git

## AI 服务商切换说明（2026-10-02）

写稿（`ai_writer.py`）与 AI 初选过滤（`ai_filter.py`）默认使用**智谱 GLM 免费模型**，原服务商保留为后备，失败时自动回退，功能不中断。

| 环境变量 | 含义 |
|---------|------|
| `ZHIPU_API_KEY` | 智谱 Key（注册 https://bigmodel.cn → API 密钥）|
| `ZHIPU_CHAT_MODEL` | 智谱免费模型名，默认 `GLM-4.7-Flash`（以官方免费模型列表为准）|

- **写稿**：优先智谱 GLM，失败/未配 Key 时回退腾讯云 `TENCENTCLOUD_API_KEY`（deepseek）。
- **AI 初选**：优先智谱 GLM，失败/未配 Key 时回退硅基流动 `SILICONFLOW_API_KEY`（Qwen）。
- **不配置 `ZHIPU_API_KEY` 时**：两个模块自动走原服务商，完全不影响原有功能。
- 免费模型名若变更：改 `config.py` 的 `ZHIPU_CHAT_MODEL`（或环境变量）即可，无需动代码逻辑。

> 注意：智谱免费模型高峰时可能出现 429「访问量过大」限流，代码已自动回退原服务商兜底。

## 设计约定

- 全部云端运行，不接任何付费 API（仅 Silicon Flow 免费额度）
- SQL 全用参数化绑定（防注入）
- 时间一律存 ISO 字符串
- 演示数据开关 `DEMO_SEED_ON_EMPTY` 在 config.py（默认 False）
- 常规选题种子在 config.py 的 ROUTINE_TOPICS_SEED（春检/安全月/七一/冬季保供/VOCs 治理等）

<!-- redeploy trigger -->

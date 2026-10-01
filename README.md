# 锦州石化投稿辅助工具（DPetroWorkbench）

零成本云端投稿辅助软件——媒体库自动采集 + 5 分钟审核 + 常规选题日历 + 选题语义对标 + 成稿体检 + 用稿规律学习。

服务对象：锦州石化企业报图文记者，给《中国石油报》《辽宁日报》投稿。

## 架构（2026-09-29 上云版）

- **应用层**：Streamlit Community Cloud（免费，1GB RAM）
- **数据层**：Supabase PostgreSQL（免费 500MB）
- **嵌入层**：Silicon Flow 免费 API（BAAI/bge-large-zh-v1.5）
- **调度层**：GitHub Actions（每日北京 07:00 自动爬取）

全部本地依赖（torch/bge）已剥离，云端启动快、内存占用低（~300MB / 1GB 上限）。

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

## 设计约定

- 全部云端运行，不接任何付费 API（仅 Silicon Flow 免费额度）
- SQL 全用参数化绑定（防注入）
- 时间一律存 ISO 字符串
- 演示数据开关 `DEMO_SEED_ON_EMPTY` 在 config.py（默认 False）
- 常规选题种子在 config.py 的 ROUTINE_TOPICS_SEED（春检/安全月/七一/冬季保供/VOCs 治理等）

<!-- redeploy trigger -->

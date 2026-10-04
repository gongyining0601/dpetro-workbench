# 项目交接说明（HANDOFF）

> ⚠️ **本文档已过期（2026-09-29 后架构整体上云）**
> - 本文档描述的是 2026-09-29 之前的**本地版**架构（SQLite + torch + 本地模型）
> - 当前架构已改为：**Supabase PostgreSQL（云端）+ Silicon Flow API 嵌入 + Streamlit Cloud 部署 + Python 3.13**
> - **请以 README.md 为准**，本文档仅保留"踩坑记录"等历史参考价值
> - 已知冲突项：数据库类型、Python 版本、向量库方案、部署方式、Tab 数量

---

> 给接手这个项目的下一个 AI 助手看。读完这份，你就能接着干。

## 项目身份

- **名称**：锦州石化投稿辅助工具（DPetroWorkbench）
- **服务对象**：锦州石化企业报图文记者，给《中国石油报》《辽宁日报》投稿
- **目标**：零成本、本地单机投稿辅助软件——媒体库自动采集 + 5 分钟审核 + 常规选题日历 + 选题对标 + 成稿体检 + 用稿规律学习
- **运行环境**：Windows / **Python 3.12.7（固定，勿用 3.13）** / 全本地，不调付费 API，不上传云端
  - 3.12 路径：`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`（与 3.13 共存，用 `py -3.12` 调用）

## 技术栈

Python 3.12 + Streamlit + SQLite + requests + BeautifulSoup + lxml。
语义对标：BAAI/bge-base-zh-v1.5（sentence-transformers + torch 2.6.0 CPU）+ 自写 numpy 暴力 cosine 向量检索（vector_store.py），**不用 chromadb**（原因见 P3）。

## 当前状态（截至 2026-09-29）

✅ **MVP 已跑通 + 真实爬取已跑通**，用户在本机验证：
- `pip install -r requirements.txt` 成功
- `streamlit run app.py` 启动成功，5 个 tab 全部可用
- **真实爬取跑通**：`py crawler.py` 抓到 36 篇真实文章入库（中国石油报 15 篇要闻 + 辽宁日报 21 篇要闻/各地），日期 2026-09-29 当期
- 爬虫架构已重构为「按 source_name 分派解析器」（见 crawler.py）

✅ **P0（真爬数据）、P1（补党建栏目）、P2（定时任务）、P3（语义向量对标）已完成**。

## 文件结构（d:\DPetroWorkbench）

| 文件 | 作用 | 状态 |
|---|---|---|
| `requirements.txt` | 依赖清单（Python 3.12，锁 torch==2.6.0） | 完整 |
| `config.py` | 媒体源/栏目/演示开关/常规选题种子 | 完整，栏目按真实版面配置，URL 已填真实地址 |
| `db.py` | SQLite 建表 + DAO | 完整，init_db 用 UPSERT 同步栏目 url |
| `crawler.py` | robots.txt 检查 + 节流 + 按 source 分派解析 | 完整，中国石油报走 epaperObject JSON，辽宁日报走 layout+con |
| `calendar_engine.py` | 常规选题日历 + 投稿命中率统计 | 可用最小版 |
| `topic_matcher.py` | 选题对标（bge 语义向量检索 + 关键词兜底）+ 角度建议 | P3 已升级 |
| `vector_store.py` | numpy 暴力 cosine 向量存储（npz 落盘） | P3 新增 |
| `draft_checker.py` | 成稿体检（正则规则）+ 三版适配 | 可用最小版 |
| `app.py` | Streamlit 审核台主入口，5 个 tab | 完整，启动时同步向量索引 |
| `run_crawler.bat` | Windows 任务计划程序启动脚本（Python 3.12 绝对路径+工作目录+UTF-8+日志重定向） | 完整 |
| `data/media.db` | SQLite 数据库（运行时生成） | 含 42 篇真实稿 |
| `data/article_vectors.npz` | bge 向量索引（只存有「相关/借鉴」审核结论的稿） | 运行时生成 |
| `data/crawler.log` | 爬虫运行日志（每次跑追加写） | 任务跑时自动生成 |
| `HANDOFF.md` | 本文件 | — |

## 数据库表

`media_source` → `column` → `article` → `review_record` 主链路；
辅助表：`routine_calendar` / `submission` / `draft_check`。
注意：`column` 是 SQLite 关键字，SQL 里必须用双引号包裹 `"column"`。

## 运行命令（Windows PowerShell，统一用 Python 3.12）

```powershell
cd d:\DPetroWorkbench
# 首次装依赖（用清华源；torch 在 Windows 上是 CPU 版）
py -3.12 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
py -3.12 -B crawler.py               # 爬真实数据（-B 避免 pyc 写入告警）
py -3.12 -B -m streamlit run app.py  # 启动审核台（首次启动会下载 bge 模型约 400MB，之后离线）
```

bge 模型首次自动从 `https://hf-mirror.com` 下载（topic_matcher.py 已设 HF_ENDPOINT），缓存在
`C:\Users\Administrator\.cache\huggingface\`，之后断网可用。

## 爬虫架构（2026-09-29 重构，按 source_name 分派）

crawler.py 三个解析器：
- **中国石油报**（`crawl_zgsyb`）：数字报是 SPA 单页应用，但 `epaperObject` JSON 作为内联 JS 字面量嵌在 SPA 页 HTML 里，含当期全部版面+文章完整数据。根入口 `http://epaper.cnpc.com.cn/zgsyb/` 返回 JS `location.replace` 重定向页（93 字节），requests 不执行 JS，需手动跟 redirect 拿当期 SPA 页。从 epaperObject 遍历 `page_XXX`，按版面 `alias` 名过滤分发文章（正文从 `content` 字段清洗，无需进详情页）。SPA 页 charset=GBK，强制 gbk 解码（apparent_encoding 会探测错）。
- **辽宁日报**（`crawl_lnd`）：服务端渲染。栏目 URL 是版面列表页模板 `.../layout/{yyyymm}/{dd}/node_XX.html`，crawler 跑时用当期日期填充，404 回退前 1-2 天。layout 页提 `con/.../content_XXX.html` 链接，进详情页提正文。详情页标题在 `<h3>`（h1/h2 空）。
- **兜底**（`crawl_generic`）：固定栏目 URL + 启发式 `extract_links`/`extract_article`，给未来新加媒体用。

`robots_allows` 改进：手动 fetch robots.txt，内容含 HTML 标签（如 SPA 重定向页）视为站点未声明 robots，放行（避免 RobotFileParser 拿到 HTML 误判禁止）。

## 待办清单（按优先级）

### ✅ P0：让爬虫真爬到数据（已完成 2026-09-29）
- 已填真实媒体 URL（config.py）
- 已写按 source 分派解析器（crawler.py）
- 已验证 36 篇真实稿入库

### ✅ P1：补党建栏目（已完成）
- 中国石油报 columns 加了「党的建设」（匹配真实版面 alias；演示稿第 5 条栏目名也从「党建」改成「党的建设」对齐）

### ✅ P2：定时自动跑（已完成 2026-09-29）
- 任务名：`DPetroWorkbench_Crawler`（已注册到 Windows 任务计划程序）
- 触发：每日 07:00，Interactive 模式（需用户登录 Windows 时才跑；错过时下次登录补跑 —— `StartWhenAvailable`）
- 启动脚本：`d:\DPetroWorkbench\run_crawler.bat`（封装 Python 绝对路径 + 工作目录 + 日志重定向到 `data\crawler.log`）
- 日志：`d:\DPetroWorkbench\data\crawler.log`（追加写，每次跑写一段，便于排查）
- Python 绝对路径：`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`（per-user 安装，py launcher 在 SYSTEM 账户下找不到，必须用绝对路径；**P3 后固定 3.12**）
- 注册命令（在**管理员 PowerShell**里跑；普通权限会 `Access is denied`）：
  ```
  schtasks /Create /TN "DPetroWorkbench_Crawler" /TR "d:\DPetroWorkbench\run_crawler.bat" /SC DAILY /ST 07:00 /IT /F
  ```
- 手动触发一次跑测试：
  ```
  schtasks /Run /TN "DPetroWorkbench_Crawler"
  ```
- ⚠ 限制：7 点电脑没开机或没登录 Windows 时任务不会跑，下次登录后自动补跑。如要后台跑（不依赖登录），需重装 Python 为 AllUsers + 重新注册任务为 SYSTEM 身份。
- ⚠ 注意：中国石油报当期第03版 alias 随期变（炼化新材料/油气新能源/理论与实践/党的建设），配了哪个抓哪个，当期没匹配就空抓。如想保证每天有党建稿，可考虑在 config 加多个 alias 候选（需小改 crawl_zgsyb 支持 alias 列表，或把第03版所有 alias 都当「党的建设」抓）。

### ✅ P3：对标器升级为语义向量检索（已完成 2026-09-29）
- **最终方案**：BAAI/bge-base-zh-v1.5（中文嵌入，768 维）+ 自写 `vector_store.py`（numpy 暴力 cosine，向量落盘 `data/article_vectors.npz`）。topic_matcher.py 从关键词字符串包含升级为语义相似检索；bge/torch 初始化失败时自动回退原关键词逻辑。
- **只对已审稿建索引**：review_record decision='相关'/'借鉴' 的稿件才进向量库。app.py 启动时调 `ensure_index_synced()` 增量同步（新增入索引、decision 改无关自动删），元数据仍以 sqlite 为唯一来源，向量库只存 article_id+向量。
- **用法**：第 4 个 tab「选题对标」用大白话描述选题即可（不用精确命中标题词），返回 0~100 相关度分。已验证语义命中准确（如"绿电 外购电"→《Hi，绿电焕新！Bye，外购网电》72 分排第一）。
- **性能**：千篇 768 维暴力矩阵乘约 3~5ms；日爬约 24 篇、年量级万篇，numpy 完全够，无需 HNSW。
- **为什么不用 chromadb（踩坑记录，勿重走）**：
  - chromadb 1.5.9 的 Rust HNSW bindings 在本机 upsert 时访问违规崩溃（0xC0000005），纯 chromadb（不碰 torch）也崩，判定为老 CPU 不兼容其新原生 bindings。
  - chromadb 0.5.x 依赖 chroma-hnswlib，在 Win + Python 3.12 无预编译 wheel，源码构建要 C++ 编译器，装不上。
  - onnxruntime 1.30 在本机 `import` 即崩（0xC0000005），降到 1.20.1 才正常（但最终 numpy 方案不需要它）。
- **Python/库版本锁定（这台机器的坑，勿升级）**：
  - 必须 **Python 3.12**：3.13 上 torch 加载 c10.dll 失败。
  - **torch==2.6.0**：2.14.0 在 Win 加载 c10.dll 崩（WinError 1114）；2.5.1 被 transformers 5.x 因 CVE-2025-32434 拒绝（要求 torch>=2.6）；2.6.0 是满足要求的最老稳定版。
  - bge 首次下载走 `HF_ENDPOINT=https://hf-mirror.com`（topic_matcher.py 已内置），huggingface.co 直连超时。
  - 注：本机 site-packages 里残留 chromadb 1.5.9 / onnxruntime 1.20.1 等包（代码已不 import，无害）；requirements.txt 不再声明它们，换机按清单装不会带上。

### P4：解析器小优化（部分完成 2026-09-29）
- ✅ **辽宁日报作者正则修复**：后处理 strip 掉「报道/报/通讯员」后缀，「记者 刘乐报道」→「记者 刘乐」、「记者 王坤报」→「记者 王坤」。21 篇里约 9 篇有作者，其余无作者（详情页作者格式多样，部分不在正文前 300 字或非「记者XX」格式，如要全提取需扩到「文/XX」「XX 报道」「通讯员 XX」等格式）。
- ✅ **中国石油报 alias 候选**：config 栏目支持 `aliases` 字段（候选列表），crawl_zgsyb 用 `alias_to_col` 映射匹配。已给「炼化新材料」加「油气新能源」候选，当期第03版「油气新能源」6 篇归到炼化新材料栏目。可按需给其他栏目加候选（如「党的建设」加「理论与实践」——需先核实理论与实践版是否含党建内容）。
- ❌ **中国石油报文章 URL 占位**（已诊断，无法优化）：站点是纯 SPA，文章对象只有 `contentid`，**没有 url/link/articleurl 字段**。试探候选 pattern（`con_{cid}.html`/`con_{cid}.htm`/`{cid}.html` 等 7 种）全部 302 重定向回 SPA 根；SPA 页 DOM 无 `id="con_{cid}"` 元素（锚点浏览器不滚动定位）；SPA 无 `hashchange` 路由（不响应锚点变化）。当前 `#con_{cid}` 锚点已是最佳近似——能打开当期 SPA 页 + 保留 contentid 信息。审核台 [app.py:91-95](file:///d:/DPetroWorkbench/app.py#L91-95) 已用 `st.text_area` 显示 body_text 前 500 字，URL 仅作"原文链接"辅助溯源，不依赖点开。**结论：保留锚点占位，不改**。

## 已知小问题（无害/待优化）

1. **中国石油报当期版面 alias 随期变**：第03版今天叫「油气新能源」，不叫「炼化新材料」或「党的建设」。config 配了 4 个栏目，当期只匹配上「要闻」。等哪期第03版叫「炼化新材料」或「党的建设」才会抓。这是版面随期变的特性，非 bug。修法见 P4。
2. **辽宁日报作者提取不全**：见 P4。
3. **中国石油报文章 URL 是锚点占位**：站点纯 SPA 无独立文章页 URL，已诊断确认（见 P4），锚点是最佳近似，不改。
4. **streamlit skills symlinks 警告**：Windows 没开 Developer Mode，skills 装到全局，不影响主程序。忽略即可。
5. **db 孤儿栏目**：旧 config 的「安全生产」「产经视线」「各地·锦州观察」「党建」「产经」「锦州观察」6 个栏目还在 db 里（url=''），init_db UPSERT 只更新同名栏目，不删旧。url 空 so 不被爬，无害。如想清理：`DELETE FROM "column" WHERE url=''`。
6. **pyc 写入沙箱告警**：Python 加载不常用编码（big5/gb18030 等）模块时写 pyc 可能被沙箱拦。用 `py -B` 禁用 pyc 可避免。功能不受影响。

## 安全合规红线（勿违反）

- 严守 robots.txt（crawler.py 的 robots_allows 已实现，且对 SPA 重定向 HTML 当无 robots 处理）
- 节流 `CRAWL_INTERVAL_SECONDS=5` 秒，可调更保守但别调更激进
- UA 已自报家门（config.py 的 USER_AGENT），**建议把 `your-email@example.com` 改成真实联系邮箱**
- 只抓公开内容，频率保守，零数据上传云端

## 设计约定（勿擅自改）

- 全部本地运行，不接任何付费 API
- SQL 全用参数化绑定（防注入）
- 时间一律存 ISO 字符串
- 演示数据开关 `DEMO_SEED_ON_EMPTY` 在 config.py（现已改 False，真实爬取跑通）
- 常规选题种子在 config.py 的 ROUTINE_TOPICS_SEED（春检/安全月/七一/冬季保供/VOCs 治理等）

## 用户偏好（来自 user_profile）

- 中文交流，代码注释也用中文
- 喜欢分步骤实施 + 中间验证
- 偏好零成本方案
- 喜欢模块化设计（共享核心 + 平台适配层）
- 偏好简洁、非技术术语的 UI

## 接手第一件事

P0/P1/P2/P3 已完成，P4 已诊断关闭（中国石油报文章 URL 占位无法优化，站点纯 SPA）。如果用户来了不知道从哪开始，建议：
- 看本文件「待办清单」，P0-P4 全部处理完毕，当前无未决待办
- 跑 `py -3.12 -B crawler.py` + `py -3.12 -B -m streamlit run app.py` 验证当前状态（应能抓到当天真实稿；语义对标在「今日审核」标几篇「相关/借鉴」后，去第 4 个 tab 体验）
- 如爬虫抓不到数据（页面结构变了），看 crawler.py 对应 source 的解析函数（crawl_zgsyb/crawl_lnd），用一次性诊断脚本抓 HTML 看结构再调
- **注意所有 Python 命令都用 `py -3.12`**（3.13 跑不了 P3 的 torch）

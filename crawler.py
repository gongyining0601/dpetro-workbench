"""礼貌爬虫：robots.txt 检查 + 节流 + 列表页/详情页解析。

架构（2026-09-29 重构，按媒体源分派解析器）：
- 中国石油报（epaper.cnpc.com.cn）：数字报是 SPA 单页应用，但 epaperObject JSON
  作为内联 JS 字面量嵌在 SPA 页 HTML 里，含当期全部版面+文章完整数据（版面 alias、
  文章 title/author/content 正文）。一次请求拿全期，无需进详情页。
- 辽宁日报（epaper.lnd.com.cn）：服务端渲染。版面列表页 node_XX.html 含本版文章
  链接（con/.../content_XXX.html），进详情页提正文。
- 未来新加媒体走 crawl_generic 兜底启发式（旧逻辑）。

安全合规：robots.txt 检查 + CRAWL_INTERVAL_SECONDS 节流，UA 自报家门。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

import ai_filter
import config
import db


@dataclass
class ArticleLink:
    title: str
    url: str


@dataclass
class ArticleContent:
    title: str
    author: str | None
    publish_date: str | None
    summary: str | None
    body_text: str | None
    has_image: bool = False


# ---------------- 网络层 ----------------

_session: requests.Session | None = None


def get_session() -> requests.Session:
    """带重试的 Session：网络抖动（超时/5xx/连接重置）自动重试 3 次。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": config.USER_AGENT})
        retry = Retry(
            total=3,
            backoff_factor=1,  # 1s, 2s, 4s 退避
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
    return _session


def fetch(url: str) -> str | None:
    """GET 一个 URL，返回文本或 None。失败优雅返回。"""
    try:
        r = get_session().get(url, timeout=config.REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"  [跳过] {url} HTTP {r.status_code}")
            return None
        # 党报数字报常见编码 gbk/utf-8 混杂；优先用响应头，失败回退 utf-8
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except requests.RequestException as e:
        print(f"  [错误] {url} -> {e}")
        return None


# ---------------- robots.txt ----------------

_robots_cache: dict[str, RobotFileParser] = {}


def robots_allows(url: str) -> bool:
    """检查 robots.txt 是否允许抓取该 URL。抓不到或非 robots 内容默认放行。

    注：部分 SPA 站点（如中国石油报）的 /robots.txt 会被重定向到 SPA 壳 HTML，
    RobotFileParser 拿到 HTML 会误判。这里手动 fetch 后先判断内容是否真是
    robots 格式（纯文本、无 HTML 标签），不像就视为站点未声明 robots，放行。
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = RobotFileParser()
        try:
            r = get_session().get(
                urljoin(base, "/robots.txt"), timeout=config.REQUEST_TIMEOUT
            )
            body = r.text or ""
            head = body[:500].lower()
            if r.status_code == 200 and "<" not in body[:500] \
               and not head.startswith(("<!", "<html", "<script", "<meta")):
                rp.parse(body.splitlines())
                _robots_cache[base] = rp
            else:
                # 404 或非 robots 内容（如 SPA 重定向 HTML）：视为无 robots，放行
                _robots_cache[base] = None  # type: ignore
        except Exception:
            # 抓不到 robots：保守起见放行（HTTP 错误视为无限制）
            _robots_cache[base] = None  # type: ignore
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(config.USER_AGENT, url)


# ---------------- 通用工具 ----------------

def content_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def clean_html_text(html: str) -> str:
    """把正文 HTML（<p>…</p>）转成纯文本。"""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


# ---------------- 兜底启发式（保留给未来媒体） ----------------

def extract_links(html: str, base_url: str) -> list[ArticleLink]:
    """兜底：从列表页提取文章链接。取所有外链到详情页的 <a>。"""
    soup = BeautifulSoup(html, "html.parser")
    links: list[ArticleLink] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = urljoin(base_url, a["href"])
        if href == base_url or href.endswith("/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(ArticleLink(title=title, url=href))
    return links


def extract_article(html: str) -> ArticleContent | None:
    """兜底：从详情页解析正文，找最长文本块容器。"""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.find("h1") or soup.find("title"))
    title = title.get_text(strip=True) if title else "(无标题)"
    candidates = soup.find_all(["div", "article", "section"])
    best = None
    best_len = 0
    for tag in candidates:
        txt = tag.get_text("\n", strip=True)
        if len(txt) > best_len:
            best_len = len(txt)
            best = txt
    body = best or soup.get_text("\n", strip=True)
    author = None
    publish_date = None
    meta_author = soup.find("meta", attrs={"name": "author"})
    if meta_author:
        author = meta_author.get("content")
    meta_date = soup.find("meta", attrs={"name": "publishdate"}) \
        or soup.find("meta", attrs={"name": "date"})
    if meta_date:
        publish_date = meta_date.get("content")
    return ArticleContent(
        title=title, author=author, publish_date=publish_date,
        summary=body[:80].replace("\n", " ") + "…", body_text=body,
    )


# ---------------- 中国石油报（SPA + epaperObject JSON） ----------------

def parse_zgsyb_object(html: str) -> dict | None:
    """从 SPA 页内联 JS 提取 epaperObject JSON 字面量。"""
    # 优先匹配带换行结尾的赋值语句
    m = re.search(r'var\s+epaperObject\s*=\s*(\{.*?\});\s*\n', html, re.S)
    if not m:
        m = re.search(r'var\s+epaperObject\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [错误] epaperObject JSON 解析失败：{e}")
        return None


def crawl_zgsyb(src: dict) -> dict:
    """中国石油报：抓根入口 redirect 到当期 SPA 页，按版面 alias 过滤分发文章。"""
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0}
    # alias -> 栏目名映射：支持一个栏目配多个 alias 候选（第03版 alias 随期变）
    alias_to_col: dict[str, str] = {}
    for c in src["columns"]:
        for a in (c.get("aliases") or [c["name"]]):
            alias_to_col[a] = c["name"]
    home = src["home"]

    if not robots_allows(home):
        print("  [robots 禁止] 中国石油报根入口")
        stats["blocked"] += 1
        return stats

    # 访问根入口，follow redirect 到当期 /zgsyb/YYYY-MM/DD/
    try:
        r = get_session().get(home, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            print(f"  [跳过] 中国石油报 HTTP {r.status_code}")
            return stats
        # 中国石油报 SPA 页 charset=GBK，强制 gbk 解码（apparent_encoding 可能探测错导致乱码）
        spa_html = r.content.decode("gbk", errors="replace")
        # 根入口可能返回 JS location.replace 重定向页（requests 不执行 JS），需手动跟
        m = re.search(r"location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)", spa_html)
        if m:
            next_url = urljoin(r.url, m.group(1))
            r = get_session().get(next_url, timeout=config.REQUEST_TIMEOUT)
            spa_html = r.content.decode("gbk", errors="replace")
        stats["fetched"] += 1
    except requests.RequestException as e:
        print(f"  [错误] 中国石油报根入口 -> {e}")
        return stats

    # 从 final URL 提取日期路径片段：r.url 形如 .../zgsyb/2026-09/29/
    parts = r.url.rstrip("/").split("/")
    date_path = "/".join(parts[-2:]) if len(parts) >= 2 else ""
    cur_date_iso = date_path.replace("/", "-")  # 2026-09-29

    obj = parse_zgsyb_object(spa_html)
    if not obj:
        print("  [错误] 未能从 SPA 页提取 epaperObject，可能页面结构变了")
        return stats
    cur_date_iso = obj.get("curDate") or cur_date_iso

    pages = obj.get("textAreaData", {}).get("pages", [])
    if not pages:
        # textAreaData 可能不在 epaperObject 里，单独找
        m2 = re.search(r'var\s+textAreaData\s*=\s*(\{.*?\});\s*\n', spa_html, re.S)
        if m2:
            try:
                pages = json.loads(m2.group(1)).get("pages", [])
            except json.JSONDecodeError:
                pass

    all_aliases = []
    for p in pages:
        bid = p.get("boardID")
        pg = obj.get(f"page_{bid}", {})
        if pg.get("alias"):
            all_aliases.append(pg["alias"])
    print(f"  当期 {cur_date_iso}，共 {len(pages)} 版：{all_aliases}")
    print(f"  按 config alias 候选过滤：{sorted(alias_to_col.keys())}")

    matched_any = False
    for p in pages:
        bid = p.get("boardID")
        page = obj.get(f"page_{bid}", {})
        alias = (page.get("alias") or "").strip()
        col_name = alias_to_col.get(alias)
        if not col_name:
            continue
        matched_any = True
        col_id = db.get_column_id(src["name"], col_name)
        if col_id is None:
            print(f"  [跳过] 未在 db 找到栏目 {src['name']}/{col_name}（重跑 db.init_db() 同步 config）")
            continue
        arts = page.get("data", [])
        print(f"  [版面 {alias}] {len(arts)} 篇")
        for a in arts:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            body_html = a.get("content") or ""
            body_text = clean_html_text(body_html)
            author = (a.get("author") or "").strip() or None
            pre = (a.get("preTitle") or "").strip()
            sub = (a.get("subtitle") or "").strip()
            summary = (pre + (" " + sub if sub else "")).strip() or body_text[:80]
            cid = a.get("contentid")
            # 文章 URL：构造锚点定位到当期 SPA 页（正文已在 body_text 里，URL 仅供审核台点开参考）
            art_url = f"http://epaper.cnpc.com.cn/zgsyb/{date_path}/#con_{cid}"
            has_image = bool(re.search(r"<img\s", body_html, re.I))
            if has_image:
                added = db.upsert_article(
                    col_id, title=title, url=art_url, author=author,
                    publish_date=cur_date_iso, summary=summary, body_text=body_text,
                    content_hash=content_hash(body_text), has_image=True,
                )
            else:
                rel, _tags = ai_filter.is_relevant(title, summary, body_text=body_text)
                if not rel:
                    stats["skipped"] += 1
                    print(f"  [AI 跳过] {title}")
                    continue
                added = db.upsert_article(
                    col_id, title=title, url=art_url, author=author,
                    publish_date=cur_date_iso, summary=summary, body_text=body_text,
                    content_hash=content_hash(body_text), has_image=False,
                )
            if added:
                stats["added"] += 1
                print(f"  + {title}")
            else:
                stats["skipped"] += 1
        time.sleep(config.CRAWL_INTERVAL_SECONDS)
    if not matched_any and all_aliases:
        print(f"  [提示] 当期没有版面 alias 命中 config 栏目名。考虑在 config.py 加这些版面之一。")
    return stats


# ---------------- 辽宁日报（layout + con 详情页） ----------------

def parse_lnd_layout(html: str, base_url: str) -> list[ArticleLink]:
    """辽宁日报版面列表页：提取所有 con/.../content_XXX.html 文章链接。"""
    soup = BeautifulSoup(html, "html.parser")
    links: list[ArticleLink] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if "/con/" in href and re.search(r"content_\d+\.html", href):
            if href in seen:
                continue
            seen.add(href)
            title = a.get_text(strip=True)
            if title:
                links.append(ArticleLink(title=title, url=href))
    return links


def parse_lnd_article(html: str) -> ArticleContent | None:
    """辽宁日报详情页：提取标题/作者/正文。"""
    soup = BeautifulSoup(html, "html.parser")
    title = None
    # 标题选择器：辽宁日报详情页标题在 <h3>（h1/h2 为空），依次试 h1→h3→标题容器→<title>
    for finder in (
        lambda: soup.find("h1"),
        lambda: soup.find("h3"),
        lambda: soup.find("div", class_=re.compile(r"article.?title|^title$", re.I)),
    ):
        t = finder()
        if t and t.get_text(strip=True):
            title = t.get_text(strip=True)
            break
    if not title:
        t = soup.find("title")
        title = t.get_text(strip=True) if t else None
    # 正文：取最长 div 文本块
    body = ""
    for div in soup.find_all("div"):
        txt = div.get_text("\n", strip=True)
        if len(txt) > len(body):
            body = txt
    if not body:
        body = soup.get_text("\n", strip=True)
    # 作者：正文前 300 字找「记者XX报道/报/通讯员」格式
    # 要求人名后紧跟「报道/报/通讯员」，避免把「记者采访了XX」误匹配为「采访了」
    author = None
    m = re.search(r'记者\s*([\u4e00-\u9fa5]{2,3})\s*(?:报道|报|通讯员)', body[:300])
    if m:
        name = m.group(1)
        if name:
            author = "记者 " + name
    # 日期：从 URL 路径 /con/YYYYMM/DD/ 提取
    publish_date = None
    m = re.search(r'/con/(\d{6})/(\d{2})/', str(soup))
    if m:
        ym, dd = m.group(1), m.group(2)
        publish_date = f"{ym[:4]}-{ym[4:6]}-{dd}"
    return ArticleContent(
        title=title or "(无标题)", author=author, publish_date=publish_date,
        summary=body[:80].replace("\n", " "), body_text=body,
    )


def crawl_lnd(src: dict) -> dict:
    """辽宁日报：按栏目 URL 模板填当期日期，抓 layout 页提 con 链接，进详情页。"""
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0}
    today = date.today()
    # 试今天 + 前 2 天（凌晨跑时今天可能没出）
    days = [today, today - timedelta(days=1), today - timedelta(days=2)]
    for col in src["columns"]:
        tmpl = col["url"]
        if "{yyyymm}" not in tmpl:
            continue
        layout_url = None
        layout_html = None
        for d in days:
            url = tmpl.format(yyyymm=d.strftime("%Y%m"), dd=d.strftime("%d"))
            if not robots_allows(url):
                stats["blocked"] += 1
                continue
            html = fetch(url)
            stats["fetched"] += 1
            if html:
                layout_url = url
                layout_html = html
                break
            time.sleep(config.CRAWL_INTERVAL_SECONDS)
        if not layout_html:
            print(f"  [跳过] {col['name']} 最近 3 天都抓不到 layout 页")
            continue
        col_id = db.get_column_id(src["name"], col["name"])
        if col_id is None:
            print(f"  [跳过] 未找到栏目 {src['name']}/{col['name']}（重跑 db.init_db() 同步 config）")
            continue
        links = parse_lnd_layout(layout_html, layout_url)[:config.CRAWL_MAX_PER_COLUMN]
        print(f"  [{col['name']}] 发现 {len(links)} 个文章链接")
        for link in links:
            time.sleep(config.CRAWL_INTERVAL_SECONDS)
            if not robots_allows(link.url):
                stats["blocked"] += 1
                continue
            detail = fetch(link.url)
            stats["fetched"] += 1
            if not detail:
                continue
            art = parse_lnd_article(detail)
            if not art or not art.title or art.title == "(无标题)":
                continue
            if art.has_image:
                added = db.upsert_article(
                    col_id, title=art.title, url=link.url, author=art.author,
                    publish_date=art.publish_date, summary=art.summary,
                    body_text=art.body_text, content_hash=content_hash(art.body_text),
                    has_image=True,
                )
            else:
                rel, _tags = ai_filter.is_relevant(art.title, art.summary, body_text=art.body_text)
                if not rel:
                    stats["skipped"] += 1
                    print(f"  [AI 跳过] {art.title}")
                    continue
                added = db.upsert_article(
                    col_id, title=art.title, url=link.url, author=art.author,
                    publish_date=art.publish_date, summary=art.summary,
                    body_text=art.body_text, content_hash=content_hash(art.body_text),
                    has_image=False,
                )
            if added:
                stats["added"] += 1
                print(f"  + {art.title}")
            else:
                stats["skipped"] += 1
    return stats


# ---------------- 兜底（未来新加媒体） ----------------

def crawl_generic(src: dict) -> dict:
    """兜底：固定栏目 URL + 启发式解析（旧逻辑）。跳过含 {yyyymm} 模板的栏目。"""
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0}
    for col in src["columns"]:
        url = col.get("url", "")
        if not url or "{" in url:
            continue
        col_id = db.get_column_id(src["name"], col["name"])
        if col_id is None:
            continue
        print(f"[{src['name']}/{col['name']}] {url}")
        if not robots_allows(url):
            print("  [robots 禁止]")
            stats["blocked"] += 1
            continue
        html = fetch(url)
        if not html:
            continue
        stats["fetched"] += 1
        links = extract_links(html, url)[:config.CRAWL_MAX_PER_COLUMN]
        print(f"  发现 {len(links)} 个候选链接")
        for link in links:
            stats["fetched"] += 1
            time.sleep(config.CRAWL_INTERVAL_SECONDS)
            if not robots_allows(link.url):
                stats["blocked"] += 1
                continue
            detail = fetch(link.url)
            if not detail:
                continue
            art = extract_article(detail)
            if not art:
                continue
            if art.has_image:
                added = db.upsert_article(
                    col_id, title=art.title, url=link.url, author=art.author,
                    publish_date=art.publish_date, summary=art.summary,
                    body_text=art.body_text, content_hash=content_hash(art.body_text),
                    has_image=True,
                )
            else:
                rel, _tags = ai_filter.is_relevant(art.title, art.summary, body_text=art.body_text)
                if not rel:
                    stats["skipped"] += 1
                    print(f"  [AI 跳过] {art.title}")
                    continue
                added = db.upsert_article(
                    col_id, title=art.title, url=link.url, author=art.author,
                    publish_date=art.publish_date, summary=art.summary,
                    body_text=art.body_text, content_hash=content_hash(art.body_text),
                    has_image=False,
                )
            if added:
                stats["added"] += 1
                print(f"  + {art.title}")
            else:
                stats["skipped"] += 1
    return stats


# ---------------- 主流程 ----------------

def crawl_all() -> dict:
    """遍历 config.MEDIA_SOURCES，按 source_name 分派解析器。返回统计。

    每个媒体源独立 try-except：单个源出错（页面结构变化、网络故障等）
    不影响其他源继续爬取，错误收集到 stats["errors"]。
    """
    stats = {"sources": 0, "fetched": 0, "added": 0, "skipped": 0, "blocked": 0, "errors": []}
    for src in config.MEDIA_SOURCES:
        stats["sources"] += 1
        name = src["name"]
        print(f"\n===== {name} =====")
        try:
            if name == "中国石油报":
                s = crawl_zgsyb(src)
            elif name == "辽宁日报":
                s = crawl_lnd(src)
            else:
                s = crawl_generic(src)
            for k in ("fetched", "added", "skipped", "blocked"):
                stats[k] += s.get(k, 0)
        except Exception as e:
            err_msg = f"{name}: {type(e).__name__}: {e}"
            print(f"[crawl_all] 媒体源出错，跳过：{err_msg}")
            stats["errors"].append(err_msg)
    return stats


# ---------------- 演示种子（无网络也能看 UI） ----------------

DEMO_ARTICLES = [
    ("中国石油报", "要闻",
     "锦州石化：春检攻坚 装置一次开车成功",
     "http://example.cnpc.com.cn/news/2026-03-15/spring-maint.html",
     "本报记者 王xx",
     "2026-03-15",
     "3月15日，锦州石化2026年春季装置大检修圆满收官，4套主要装置一次开车成功…",
     "3月15日，锦州石化2026年春季装置大检修圆满收官。本次检修历时32天，涉及催化裂化、连续重整、加氢裂化、硫磺回收等4套主要装置。公司党员先锋岗带头开展'小改小革'，对换热器管束清洗工艺进行优化，单台节约蒸汽1.2吨/小时。检修期间累计完成VOCs治理改造项目3项，LDAR检测点整改合格率100%。"),
    ("中国石油报", "炼化新材料",
     "锦州石化国ⅥB汽油月产量创新高",
     "http://example.cnpc.com.cn/news/2026-03-20/gasoline-record.html",
     "本报记者 李xx",
     "2026-03-20",
     "锦州石化3月国ⅥB车用汽油月产量达到18.5万吨，环比增长8.2%，创历史新高…",
     "锦州石化3月国ⅥB车用汽油月产量达到18.5万吨，环比增长8.2%，创单月历史新高。催化裂化装置通过优化反应温度与剂油比，辛烷值提升1.2个单位；MTBE装置满负荷运行，烷基化油比例稳步提升至12%。"),
    ("辽宁日报", "要闻",
     "锦州石化储气库日注气量突破800万方",
     "http://example.lnd.com.cn/news/2026-10-20/gas-storage.html",
     "本报记者 张xx",
     "2026-10-20",
     "锦州石化配套储气库进入冬供前注气冲刺阶段，日注气量首破800万方…",
     "10月20日，锦州石化配套储气库进入冬供前注气冲刺阶段，日注气量首次突破800万方，为辽宁及东北地区冬季天然气保供备足'粮草'。今年累计注气已达4.6亿方，同比增长12%。"),
    ("辽宁日报", "各地",
     "锦州石化'小改小革'年降本3200万",
     "http://example.lnd.com.cn/news/2026-09-01/innovation.html",
     "本报记者 赵xx",
     "2026-09-01",
     "1-8月锦州石化员工'小改小革'项目累计落地157项，降本增效3200万元…",
     "1-8月，锦州石化员工'小改小革'项目累计落地157项，降本增效3200万元。其中重整装置节能优化、加氢装置催化剂再生延长寿命等10项典型项目获公司技改奖。"),
    ("中国石油报", "党的建设",
     "锦州石化'党员先锋岗'助力装置春检",
     "http://example.cnpc.com.cn/news/2026-03-25/party-pioneer.html",
     "本报记者 王xx",
     "2026-03-25",
     "春检期间，锦州石化48个'党员先锋岗'带头攻坚，承担检修关键节点10项…",
     "春检期间，锦州石化48个'党员先锋岗'带头攻坚，承担检修关键节点10项，开展主题党日12场。催化裂化装置检修队临时党支部获评'检修先锋党支部'。"),
]


def seed_demo_data() -> int:
    """当 DB 无文章时灌入演示数据，便于先看 UI。返回新增条数。"""
    added = 0
    for src_name, col_name, title, url, author, pdate, summary, body in DEMO_ARTICLES:
        col_id = db.get_column_id(src_name, col_name)
        if col_id is None:
            print(f"  [跳过] 未找到栏目 {src_name}/{col_name}")
            continue
        if db.upsert_article(
            col_id, title=title, url=url, author=author,
            publish_date=pdate, summary=summary, body_text=body,
            content_hash=content_hash(body),
        ):
            added += 1
    print(f"演示数据已灌入 {added} 条")
    return added


def main():
    db.init_db()
    # 若库为空且开启了演示模式，先灌演示数据
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute("SELECT COUNT(*) AS n FROM article")
        n = cur.fetchone()["n"]
    if n == 0 and config.DEMO_SEED_ON_EMPTY:
        print("数据库为空，先灌入演示数据。要爬真实数据请关闭 DEMO_SEED_ON_EMPTY 并跑 py crawler.py。")
        seed_demo_data()
        return
    print("开始爬取…")
    stats = crawl_all()
    print(f"\n完成：媒体源 {stats['sources']}，抓取 {stats['fetched']}，"
          f"新增 {stats['added']}，跳过(已存在) {stats['skipped']}，robots拦截 {stats['blocked']}")


if __name__ == "__main__":
    main()

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
from urllib3.exceptions import InsecureRequestWarning

# 证书域名不匹配的站点统一走 http（见 _normalize_url），无需 verify=False
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
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
    image_urls: str | None = None


# ---------------- 网络层 ----------------

_session: requests.Session | None = None
_session_no_ssl: requests.Session | None = None


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


def get_session_no_ssl() -> requests.Session:
    """不校验 SSL 的 Session（备用，当前所有站点走 http，暂未使用）。

    不挂 Retry 适配器，SSL 失败立即返回，避免 3 次重试+退避拖慢爬虫。
    """
    global _session_no_ssl
    if _session_no_ssl is None:
        _session_no_ssl = requests.Session()
        _session_no_ssl.headers.update({"User-Agent": config.USER_AGENT})
        _session_no_ssl.verify = False
    return _session_no_ssl


def _need_no_ssl(url: str) -> bool:
    """判断 URL 是否需要跳过 SSL 校验。

    ccin.com.cn 已统一用 http（config.py），不再需要 verify=False。
    此函数保留给未来其他 SSL 有问题的站点使用。
    """
    return False


def _detect_encoding(r, url: str = "") -> str:
    """三级编码探测，解决党报数字报乱码问题。

    优先级：
    1. HTML <meta charset> 标签（最可靠，页面作者声明）
    2. Content-Type 响应头 charset
    3. requests apparent_encoding（chardet 探测）
    4. 按域名兜底：人民日报/辽宁日报 → gbk，中国化工报 → utf-8
    """
    # 1. 从 HTML meta 标签读取 charset
    m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?([\w-]+)', r.content[:2000], re.I)
    if m:
        enc = m.group(1).decode("ascii", errors="ignore").lower()
        if enc in ("gbk", "gb2312", "gb18030", "utf-8", "utf8"):
            return enc
    # 2. Content-Type 响应头
    if r.encoding and r.encoding.lower() not in ("iso-8859-1", "ascii"):
        return r.encoding
    # 3. chardet 探测
    if r.apparent_encoding:
        enc = r.apparent_encoding.lower()
        if enc in ("gbk", "gb2312", "gb18030", "utf-8", "utf8"):
            return enc
    # 4. 按域名兜底
    domain = urlparse(url).netloc.lower() if url else ""
    if any(d in domain for d in ("people.com.cn", "lnd.com.cn")):
        return "gbk"
    if "ccin.com.cn" in domain:
        return "utf-8"
    return "utf-8"


def fetch(url: str) -> str | None:
    """GET 一个 URL，返回文本或 None。失败优雅返回。

    所有站点统一走 http（config.py），默认用普通 session（verify=True）。
    编码探测增强：优先 meta charset，避免党报 GBK 页面被误判为 ISO-8859-1 导致乱码。
    """
    sess = get_session_no_ssl() if _need_no_ssl(url) else get_session()
    try:
        r = sess.get(url, timeout=config.REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [错误] {url} -> {e}")
        return None
    if r.status_code != 200:
        print(f"  [跳过] {url} HTTP {r.status_code}")
        return None
    # 三级编码探测：meta charset > 响应头 > chardet > 域名兜底
    enc = _detect_encoding(r, url)
    r.encoding = enc
    return r.text


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
            sess = get_session_no_ssl() if _need_no_ssl(url) else get_session()
            r = sess.get(
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


def extract_caption_or_short(soup, body_text: str) -> str:
    """提取图片新闻的「照片说明+短文字」，弃长正文。

    优先级：
    1. <img> 的 alt/title 属性（图注，最准）
    2. 图注容器：figcaption / .pic-caption / .pictext / .caption 等
    3. 兜底：正文首段 ≤200 字
    """
    captions = []
    # 1. img alt/title
    for img in soup.find_all("img"):
        for attr in ("alt", "title"):
            v = (img.get(attr) or "").strip()
            if len(v) >= 4 and v not in captions:
                captions.append(v)
    # 2. 图注容器
    for sel in (
        "figcaption",
        "p[class*=caption]", "p[class*=pic]", "p[class*=img]",
        "div[class*=caption]", "div[class*=pic]", "div[class*=img]",
        "span[class*=caption]",
    ):
        try:
            for el in soup.select(sel):
                t = el.get_text(strip=True)
                if len(t) >= 4 and t not in captions:
                    captions.append(t)
        except Exception:
            pass
    if captions:
        return "；".join(captions)
    # 3. 兜底：首段 ≤200 字
    if body_text:
        first = body_text.split("\n")[0].strip()
        if first:
            return first[:200]
    return (body_text or "")[:200]


def classify_article(src_name: str, col_name: str, title: str, body_text: str = "") -> str | None:
    """判定文章是否属于 5 类之一。返回类别名或 None（不属于则丢弃）。

    主路径：CATEGORY_MAP 按「源/栏目」精确匹配（零 API 成本）
    兜底：标题关键词匹配
    """
    sc = f"{src_name}/{col_name}"
    # 主路径
    for cat, sc_set in config.CATEGORY_MAP.items():
        if sc in sc_set:
            return cat
    # 兜底：标题关键词
    blob = f"{title} {body_text[:100]}"
    for cat, kws in config.CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in blob:
                return cat
    return None


# ---------------- URL 规范化（SSL 证书不匹配的域名 https→http） ----------------

# 这些域名的 SSL 证书与主机名不匹配，浏览器加载时报错；HTTP 可正常访问，统一改写。
_SSL_BROKEN_DOMAINS = ("ccin.com.cn",)


def _normalize_url(url: str) -> str:
    """把 SSL 证书有问题的域名的 https 改成 http，避免浏览器控制台 SSL 警告。"""
    if not url or not url.startswith("https://"):
        return url
    for domain in _SSL_BROKEN_DOMAINS:
        if domain in url:
            return "http://" + url[len("https://"):]
    return url


def _lookback_days(n: int = 7) -> list[date]:
    """返回最近 n 天的日期列表（含今天），用于党报休刊期回溯抓取。"""
    today = date.today()
    return [today - timedelta(days=i) for i in range(n)]


# ---------------- 兜底启发式（保留给未来媒体） ----------------

def extract_links(html: str, base_url: str) -> list[ArticleLink]:
    """兜底：从列表页提取文章链接。只保留文章类 URL，过滤分类页/导航链接。"""
    soup = BeautifulSoup(html, "html.parser")
    links: list[ArticleLink] = []
    seen: set[str] = set()
    # 文章 URL 特征：含 /detail/ /content/ /news/ /article/ 或数字ID
    article_re = re.compile(r"(/detail/|/content/|/news/|/article/|/\d{6,})", re.I)
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = urljoin(base_url, a["href"])
        if href == base_url or href.endswith("/"):
            continue
        # 只保留文章类 URL
        if not article_re.search(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(ArticleLink(title=title, url=href))
    return links




def _extract_image_urls(soup, base_url: str = "") -> tuple[bool, str | None]:
    """从 BeautifulSoup 对象提取所有 img 的 src，返回 (是否有图, JSON字符串)。

    base_url 不为空时，相对路径自动转绝对路径（urljoin）。
    过滤掉站点 logo/UI 图标、整版报纸缩略图、广告图，只保留正文图片新闻照片。
    """
    imgs = soup.find_all("img")
    urls = []
    # 整版报纸版面缩略图关键词（整版页面的小图，不是新闻照片）
    _board_thumb_kws = (
        "board", "page_", "page-", "thumb", "small", "mini", "icon_",
        "nav_", "nav-", "banner", "ad_", "ad-", "qrcode", "qr_", "ewm",
        "sprite", "btn_", "arrow", "go_top", "down.", "loading",
    )
    # 站点公共资源目录关键词
    _public_res_kws = (
        "logo", "icon", "d1.gif", "d.gif", "files/", "qrapp", "slogen",
        "/images/web/", "/webinc/", "header", "footer", "/nav/",
    )
    for img in imgs:
        s = img.get("src") or img.get("data-src") or img.get("data-original")
        if not s or s.startswith("data:"):
            continue
        sl = s.lower()
        # 过滤 logo/UI 图标/站点公共资源
        if any(x in sl for x in _public_res_kws):
            continue
        # 过滤整版报纸版面缩略图、广告图、导航图
        if any(x in sl for x in _board_thumb_kws):
            continue
        # 只保留图片扩展名
        if not re.search(r"\.(jpg|jpeg|png|gif|webp)(\.|\?|$)", sl):
            continue
        # 过滤过小的缩略图（文件名含尺寸暗示，如 _s. _thumb. _100x100）
        if re.search(r"(_s|_thumb|_\d{2,3}x\d{2,3})\.(jpg|jpeg|png|gif)", sl):
            continue
        if base_url:
            s = urljoin(base_url, s)
        # SSL 证书不匹配的域名（如 ccin.com.cn）统一 https→http，避免浏览器警告
        s = _normalize_url(s)
        if s not in urls:
            urls.append(s)
    if not urls:
        return False, None
    return True, json.dumps(urls, ensure_ascii=False)


def _is_photo_news(soup, body_text: str, image_count: int,
                   title: str = "", url: str = "") -> bool:
    """两层判定是否为图片新闻。

    第一层：确认是一条新闻（过滤广告/公告/列表页）
      - 标题 4~50 字，不含「公告/声明/通知/广告/招聘/启事」等非新闻词
      - 正文 > 50 字
    第二层：确认是图片新闻（有图 + 有图注/多图/短正文）
      - 必须有图（image_count >= 1）
      - 满足以下任一：
        (a) 有图注（figcaption 或 img alt/title >= 4 字）
        (b) 图片数 >= 2（组照）
        (c) 正文 <= 500 字（短图文）
    """
    body = (body_text or "").strip()
    body_len = len(body)
    title_clean = (title or "").strip()

    # ---------- 第一层：是新闻吗？ ----------
    # 标题长度不合理
    if len(title_clean) < 4 or len(title_clean) > 60:
        return False
    # 非新闻标题关键词
    _non_news_kws = ("公告", "声明", "通知", "广告", "招聘", "启事", "寻人", "寻物",
                     "致歉", "更正", "鸣谢", "讣告", "婚讯", "寿辰",
                     "本版责编", "本版编辑", "责编：", "责任编辑", "版式策划",
                     "图片编辑", "美术编辑", "校检", "审读")
    if any(kw in title_clean for kw in _non_news_kws):
        return False
    # 正文过短（不是新闻）
    if body_len < 50:
        return False

    # ---------- 第二层：是图片新闻吗？ ----------
    if image_count < 1:
        return False
    # (a) 有图注
    has_caption = False
    for sel in ("figcaption", "p[class*=caption]", "p[class*=pic]",
                "div[class*=caption]", "div[class*=pic]", "span[class*=caption]"):
        try:
            if soup.select(sel):
                has_caption = True
                break
        except Exception:
            pass
    if not has_caption:
        for img in soup.find_all("img"):
            for attr in ("alt", "title"):
                v = (img.get(attr) or "").strip()
                if len(v) >= 4:
                    has_caption = True
                    break
            if has_caption:
                break
    if has_caption:
        return True
    # (b) 多图组照
    if image_count >= 2:
        return True
    # (c) 短正文
    if body_len <= 500:
        return True
    return False


def extract_article(html: str, base_url: str = "") -> ArticleContent | None:
    """兜底：从详情页解析正文，找最长文本块容器。"""
    soup = BeautifulSoup(html, "html.parser")
    has_image, image_urls = _extract_image_urls(soup, base_url)
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
        candidate = (meta_author.get("content") or "").strip()
        # 过滤掉不像真名的 meta author（如 ccin 的 "TOPQH" 站点 ID：全 ASCII 无中文）
        if candidate and re.search(r"[\u4e00-\u9fa5]", candidate):
            author = candidate
    # author 兜底：从正文前 300 字找"记者XX/通讯员XX/作者：XX"
    if not author:
        m = re.search(
            r'记者\s*([\u4e00-\u9fa5]{2,3})|通讯员\s*([\u4e00-\u9fa5]{2,3})|作者[：:]\s*([\u4e00-\u9fa5]{2,4})',
            body[:300],
        )
        if m:
            author = " ".join(g for g in m.groups() if g)
    meta_date = soup.find("meta", attrs={"name": "publishdate"}) \
        or soup.find("meta", attrs={"name": "date"})
    if meta_date:
        publish_date = meta_date.get("content")
    # publish_date 兜底：从详情页 .date/.time/.pubtime 元素或正文 regex 提取
    if not publish_date:
        for cls in ("date", "time", "pubtime", "pub-date", "article-time"):
            el = soup.find(attrs={"class": re.compile(cls, re.I)})
            if el:
                t = el.get_text(strip=True)
                # 兼容 "2026-09-22" 和 "2026年09月22日"
                m = re.search(r'(\d{4})[-年](\d{1,2})[月-](\d{1,2})', t)
                if m:
                    publish_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    break
    if not publish_date:
        # 兼容 "2026-09-22" 和 "2026年09月22日"
        m = re.search(r'(\d{4})[-年](\d{1,2})[月-](\d{1,2})', body[:200])
        if m:
            publish_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ArticleContent(
        title=title, author=author, publish_date=publish_date,
        summary=body[:80].replace("\n", " ") + "…", body_text=body,
        has_image=has_image, image_urls=image_urls,
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


def crawl_zgsyb(src: dict, target_date: date | None = None) -> dict:
    """中国石油报：抓根入口 redirect 到当期 SPA 页，按版面 alias 过滤分发文章。

    target_date: 指定日期抓取（用于历史回填）；None 则抓当期最新。
    """
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0}
    # alias -> 栏目名映射：支持一个栏目配多个 alias 候选（第03版 alias 随期变）
    alias_to_col: dict[str, str] = {}
    for c in src["columns"]:
        for a in (c.get("aliases") or [c["name"]]):
            alias_to_col[a] = c["name"]

    # 构造目标日期的 SPA 页 URL
    if target_date:
        date_path = target_date.strftime("%Y-%m/%d")
        url = f"http://epaper.cnpc.com.cn/zgsyb/{date_path}/"
    else:
        url = src["home"]

    if not robots_allows(url):
        print(f"  [robots 禁止] 中国石油报 {url}")
        stats["blocked"] += 1
        return stats

    # 访问入口，follow redirect 到当期 /zgsyb/YYYY-MM/DD/
    try:
        r = get_session().get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            print(f"  [跳过] 中国石油报 HTTP {r.status_code}（{target_date or '当期'}）")
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
        print(f"  [错误] 中国石油报 -> {e}")
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

    # 【修复日期错位】指定 target_date 时，校验服务端返回的当期日期是否一致。
    # 不一致说明该日期休刊，服务端重定向到了最近一期，应跳过而非用错日期入库。
    if target_date and cur_date_iso and cur_date_iso != target_date.isoformat():
        print(f"  [跳过] 目标 {target_date} 但当期为 {cur_date_iso}，该日期休刊")
        return stats
    if target_date:
        publish_date = target_date.isoformat()
    else:
        publish_date = cur_date_iso

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
            # 中国石油报图片不在正文 HTML 里，而在 imageinfo[].path 字段（相对路径）。
            # imgPrefix 通常是 "res/"，需与当期版面 URL 拼接成绝对地址。
            img_urls: list[str] = []
            img_prefix = obj.get("imgPrefix", "") or ""
            base_for_img = f"http://epaper.cnpc.com.cn/zgsyb/{date_path}/"
            for info in (a.get("imageinfo") or []):
                p = (info.get("path") or info.get("preview") or "").strip()
                if p:
                    full = urljoin(base_for_img, img_prefix + p)
                    img_urls.append(full)
            # 兜底：正文 HTML 里的 <img> 标签
            has_body_img, body_img_urls = _extract_image_urls(
                BeautifulSoup(body_html, "html.parser"), art_url
            )
            if has_body_img and body_img_urls:
                try:
                    img_urls.extend(json.loads(body_img_urls))
                except (json.JSONDecodeError, TypeError):
                    pass
            has_image = bool(img_urls)
            image_urls = json.dumps(img_urls, ensure_ascii=False) if img_urls else None
            # 【新规则1】两层判定：先确认是新闻，再确认是图片新闻
            if not _is_photo_news(BeautifulSoup(body_html, "html.parser"), body_text,
                                  len(img_urls), title=title, url=art_url):
                stats["skipped_no_image"] = stats.get("skipped_no_image", 0) + 1
                continue
            # 【新规则2】只保留 5 类
            cat = classify_article(src["name"], col_name, title, body_text)
            if not cat:
                stats["skipped_category"] = stats.get("skipped_category", 0) + 1
                continue
            # 【新规则3】只存图注/短说明，弃长正文
            short_text = extract_caption_or_short(BeautifulSoup(body_html, "html.parser"), body_text)
            added = db.upsert_article(
                col_id, title=title, url=_normalize_url(art_url), author=author,
                publish_date=publish_date, summary=short_text[:80], body_text=short_text,
                content_hash=content_hash(short_text), has_image=True, image_urls=image_urls,
            )
            if added:
                stats["added"] += 1
                print(f"  + [{cat}] {title}")
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


def parse_lnd_article(html: str, url: str = "") -> ArticleContent | None:
    """辽宁日报详情页：提取标题/作者/正文。日期从 URL 提取。"""
    soup = BeautifulSoup(html, "html.parser")
    has_image, image_urls = _extract_image_urls(soup, url)
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
    # 作者：搜正文开头 300 字 + 结尾 300 字（党报署名常在文末，如"本报记者 陶阳 文"）
    author = None
    author_haystack = body[:300] + "\n" + body[-300:]
    m = re.search(
        r'记者\s*([\u4e00-\u9fa5]{2,3})|通讯员\s*([\u4e00-\u9fa5]{2,3})|作者[：:]\s*([\u4e00-\u9fa5]{2,4})',
        author_haystack,
    )
    if m:
        name = next((g for g in m.groups() if g), None)
        if name:
            author = name
    # 日期：从文章 URL 路径 /con/YYYYMM/DD/ 提取
    publish_date = None
    m = re.search(r'/con/(\d{6})/(\d{2})/', url)
    if m:
        ym, dd = m.group(1), m.group(2)
        publish_date = f"{ym[:4]}-{ym[4:6]}-{dd}"
    return ArticleContent(
        title=title or "(无标题)", author=author, publish_date=publish_date,
        summary=body[:80].replace("\n", " "), body_text=body,
        has_image=has_image, image_urls=image_urls,
    )


def crawl_lnd(src: dict, target_date: date | None = None) -> dict:
    """辽宁日报：按栏目 URL 模板填当期日期，抓 layout 页提 con 链接，进详情页。

    target_date: 指定日期抓取；None 则回溯最近 7 天（覆盖休刊期）。
    """
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0,
             "skipped_no_image": 0, "skipped_category": 0}
    if target_date:
        days = [target_date]
    else:
        # 回溯 7 天：覆盖十一/春节等党报休刊长假，凌晨跑时今天可能还没出
        days = _lookback_days(7)
    for col in src["columns"]:
        tmpl = col["url"]
        if "{yyyymm}" not in tmpl:
            continue
        col_name = col["name"]
        layout_url = None
        layout_html = None
        # 辽宁日报版号随期变（各地可能在 node_04/node_07 或不存在），
        # 先抓头版 node_01 的版面导航，按版名动态匹配真实 node 号。
        for d in days:
            yyyymm = d.strftime("%Y%m")
            dd = d.strftime("%d")
            base = f"https://epaper.lnd.com.cn/lnrbepaper/pc/layout/{yyyymm}/{dd}/"
            nav_url = base + "node_01.html"
            if not robots_allows(nav_url):
                stats["blocked"] += 1
                continue
            nav_html = fetch(nav_url)
            stats["fetched"] += 1
            if not nav_html:
                time.sleep(config.CRAWL_INTERVAL_SECONDS)
                continue
            nav_soup = BeautifulSoup(nav_html, "html.parser")
            actual_node = None
            for a in nav_soup.select("a[href*=node_]"):
                link_text = a.get_text(strip=True)
                if col_name in link_text:
                    actual_node = a.get("href", "")
                    break
            if actual_node:
                url = urljoin(base, actual_node)
            else:
                # 当天无此版面，回退用配置的硬编码版号（可能 404，外层会跳过）
                url = tmpl.format(yyyymm=yyyymm, dd=dd)
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
            print(f"  [跳过] {col_name} 最近 3 天都抓不到 layout 页")
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
            art = parse_lnd_article(detail, link.url)
            if not art or not art.title or art.title == "(无标题)":
                continue
            # 日期校验：指定 target_date 时，文章日期必须匹配（防重定向到其他期）
            if target_date and art.publish_date and art.publish_date != target_date.isoformat():
                stats["skipped"] += 1
                continue
            # 【新规则1】严格图片新闻判定：≥1 张图 且(有图注 OR 正文 ≤500 字)
            _lnd_soup = BeautifulSoup(detail, "html.parser")
            _lnd_img_count = len(json.loads(art.image_urls)) if art.image_urls else 0
            if not _is_photo_news(_lnd_soup, art.body_text, _lnd_img_count,
                                  title=art.title, url=link.url):
                stats["skipped_no_image"] = stats.get("skipped_no_image", 0) + 1
                continue
            # 【新规则2】只保留 5 类
            cat = classify_article(src["name"], col["name"], art.title, art.body_text)
            if not cat:
                stats["skipped_category"] = stats.get("skipped_category", 0) + 1
                continue
            # 【新规则3】只存图注/短说明，弃长正文
            short_text = extract_caption_or_short(_lnd_soup, art.body_text)
            added = db.upsert_article(
                col_id, title=art.title, url=_normalize_url(link.url), author=art.author,
                publish_date=art.publish_date, summary=short_text[:80],
                body_text=short_text, content_hash=content_hash(short_text),
                has_image=True, image_urls=art.image_urls,
            )
            if added:
                stats["added"] += 1
                print(f"  + [{cat}] {art.title}")
            else:
                stats["skipped"] += 1
    return stats


# ---------------- 兜底（未来新加媒体） ----------------

def crawl_generic(src: dict, date_range: tuple[date, date] | None = None) -> dict:
    """兜底：固定栏目 URL + 启发式解析（旧逻辑）。跳过含 {yyyymm} 模板的栏目。

    date_range: (start, end) 日期范围过滤，仅入库 publish_date 在此范围内的文章。
    """
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0,
             "skipped_no_image": 0, "skipped_category": 0}
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
            art = extract_article(detail, link.url)
            if not art:
                continue
            # 二次校验：排除分类页（标题含"首页"或 URL 不像文章）
            if "首页" in art.title or "/detail/" not in link.url:
                stats["skipped"] += 1
                continue
            # 日期范围过滤（新闻站用）
            if date_range and art.publish_date:
                try:
                    ad = date.fromisoformat(art.publish_date)
                    if not (date_range[0] <= ad <= date_range[1]):
                        stats["skipped"] += 1
                        continue
                except (ValueError, TypeError):
                    pass
            # 【新规则1】严格图片新闻判定
            _gen_soup = BeautifulSoup(detail, "html.parser")
            _gen_img_count = len(json.loads(art.image_urls)) if art.image_urls else 0
            if not _is_photo_news(_gen_soup, art.body_text, _gen_img_count,
                                  title=art.title, url=link.url):
                stats["skipped_no_image"] += 1
                continue
            # 【新规则2】只保留 5 类
            cat = classify_article(src["name"], col["name"], art.title, art.body_text)
            if not cat:
                stats["skipped_category"] += 1
                continue
            # 【新规则3】只存图注/短说明，弃长正文
            short_text = extract_caption_or_short(_gen_soup, art.body_text)
            added = db.upsert_article(
                col_id, title=art.title, url=_normalize_url(link.url), author=art.author,
                publish_date=art.publish_date, summary=short_text[:80],
                body_text=short_text, content_hash=content_hash(short_text),
                has_image=True, image_urls=art.image_urls,
            )
            if added:
                stats["added"] += 1
                print(f"  + [{cat}] {art.title}")
            else:
                stats["skipped"] += 1
    return stats


# ---------------- 人民日报（layout + content，类辽宁日报结构） ----------------

def parse_rmrb_layout(html: str, base_url: str) -> list[ArticleLink]:
    """人民日报版面列表页：提取 content_XXX.html 文章链接。"""
    soup = BeautifulSoup(html, "html.parser")
    links: list[ArticleLink] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if "/content/" in href and re.search(r"content_\d+\.html", href):
            if href in seen:
                continue
            seen.add(href)
            title = a.get_text(strip=True)
            if title and len(title) >= 4:
                links.append(ArticleLink(title=title, url=href))
    return links


def parse_rmrb_article(html: str, url: str = "") -> ArticleContent | None:
    """人民日报详情页：提取标题/作者/正文/图片。日期从 URL 提取。"""
    soup = BeautifulSoup(html, "html.parser")
    has_image, image_urls = _extract_image_urls(soup, url)
    # 标题：h1 → h2 → .article-title → title
    title = None
    for finder in (
        lambda: soup.find("h1"),
        lambda: soup.find("h2"),
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
    # 作者：搜正文开头 300 字 + 结尾 300 字（党报署名常在文末，如"本报记者 陶阳 文"）
    author = None
    author_haystack = body[:300] + "\n" + body[-300:]
    m = re.search(
        r'记者\s*([\u4e00-\u9fa5]{2,3})|通讯员\s*([\u4e00-\u9fa5]{2,3})|作者[：:]\s*([\u4e00-\u9fa5]{2,4})',
        author_haystack,
    )
    if m:
        name = next((g for g in m.groups() if g), None)
        if name:
            author = name
    # 日期：从 URL /content/YYYYMM/DD/ 提取
    publish_date = None
    m = re.search(r'/content/(\d{6})/(\d{2})/', url)
    if m:
        ym, dd = m.group(1), m.group(2)
        publish_date = f"{ym[:4]}-{ym[4:6]}-{dd}"
    return ArticleContent(
        title=title or "(无标题)", author=author, publish_date=publish_date,
        summary=body[:80].replace("\n", " "), body_text=body,
        has_image=has_image, image_urls=image_urls,
    )


def crawl_rmrb(src: dict, target_date: date | None = None) -> dict:
    """人民日报：按栏目 URL 模板填当期日期，抓 layout 页提 content 链接，进详情页。

    target_date: 指定日期抓取；None 则回溯最近 7 天。
    """
    stats = {"fetched": 0, "added": 0, "skipped": 0, "blocked": 0,
             "skipped_no_image": 0, "skipped_category": 0}
    if target_date:
        days = [target_date]
    else:
        # 回溯 7 天：覆盖十一/春节等党报休刊长假
        days = _lookback_days(7)
    for col in src["columns"]:
        tmpl = col["url"]
        if "{yyyymm}" not in tmpl:
            continue
        col_name = col["name"]
        layout_url = None
        layout_html = None
        # 人民日报版号随期变，先抓头版 node_01 的版面导航，按版名动态匹配真实 node 号。
        for d in days:
            yyyymm = d.strftime("%Y%m")
            dd = d.strftime("%d")
            base = f"http://paper.people.com.cn/rmrb/pc/layout/{yyyymm}/{dd}/"
            nav_url = base + "node_01.html"
            if not robots_allows(nav_url):
                stats["blocked"] += 1
                continue
            nav_html = fetch(nav_url)
            stats["fetched"] += 1
            if not nav_html:
                time.sleep(config.CRAWL_INTERVAL_SECONDS)
                continue
            nav_soup = BeautifulSoup(nav_html, "html.parser")
            actual_node = None
            for a in nav_soup.select("a[href*=node_]"):
                link_text = a.get_text(strip=True)
                if col_name in link_text:
                    actual_node = a.get("href", "")
                    break
            if actual_node:
                url = urljoin(base, actual_node)
            else:
                # 当天无此版面，回退用配置的硬编码版号（可能 404，外层会跳过）
                url = tmpl.format(yyyymm=yyyymm, dd=dd)
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
            print(f"  [跳过] {col_name} 最近 3 天都抓不到 layout 页")
            continue
        col_id = db.get_column_id(src["name"], col["name"])
        if col_id is None:
            print(f"  [跳过] 未找到栏目 {src['name']}/{col['name']}")
            continue
        links = parse_rmrb_layout(layout_html, layout_url)[:config.CRAWL_MAX_PER_COLUMN]
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
            art = parse_rmrb_article(detail, link.url)
            if not art or not art.title or art.title == "(无标题)":
                continue
            # 日期校验：指定 target_date 时，文章日期必须匹配（防重定向到其他期）
            if target_date and art.publish_date and art.publish_date != target_date.isoformat():
                stats["skipped"] += 1
                continue
            # 【新规则1】严格图片新闻判定：≥1 张图 且(有图注 OR 正文 ≤500 字)
            _rmrb_soup = BeautifulSoup(detail, "html.parser")
            _rmrb_img_count = len(json.loads(art.image_urls)) if art.image_urls else 0
            if not _is_photo_news(_rmrb_soup, art.body_text, _rmrb_img_count,
                                  title=art.title, url=link.url):
                stats["skipped_no_image"] += 1
                continue
            # 【新规则2】只保留 5 类
            cat = classify_article(src["name"], col["name"], art.title, art.body_text)
            if not cat:
                stats["skipped_category"] += 1
                continue
            # 【新规则3】只存图注/短说明
            short_text = extract_caption_or_short(_rmrb_soup, art.body_text)
            added = db.upsert_article(
                col_id, title=art.title, url=_normalize_url(link.url), author=art.author,
                publish_date=art.publish_date, summary=short_text[:80],
                body_text=short_text, content_hash=content_hash(short_text),
                has_image=True, image_urls=art.image_urls,
            )
            if added:
                stats["added"] += 1
                print(f"  + [{cat}] {art.title}")
            else:
                stats["skipped"] += 1
    return stats


# ---------------- 中国化工报（中化新网新闻列表，走 generic 解析） ----------------

def crawl_ccin(src: dict, date_range: tuple[date, date] | None = None) -> dict:
    """中国化工报（中化新网）：新闻列表页 + 详情页，应用图片新闻+5类过滤。

    复用 crawl_generic 的解析逻辑，但因为 ccin.com.cn 是新闻站（非数字报），
    文章 URL 形如 /detail/{id}/news，直接走 extract_links + extract_article。

    date_range: (start, end) 日期范围过滤（新闻站无日期 URL，靠文章发布日期过滤）。
    """
    return crawl_generic(src, date_range=date_range)


# ---------------- 主流程 ----------------

def crawl_all(target_date: date | None = None) -> dict:
    """遍历 config.MEDIA_SOURCES，按 source_name 分派解析器。返回统计。

    target_date: 指定日期抓取（数字报用）；None 则抓当期最新。
    每个媒体源独立 try-except：单个源出错不影响其他源。
    """
    stats = {"sources": 0, "fetched": 0, "added": 0, "skipped": 0, "blocked": 0, "errors": []}
    for src in config.MEDIA_SOURCES:
        stats["sources"] += 1
        name = src["name"]
        print(f"\n===== {name} =====")
        try:
            if name == "中国石油报":
                s = crawl_zgsyb(src, target_date=target_date)
            elif name == "辽宁日报":
                s = crawl_lnd(src, target_date=target_date)
            elif name == "人民日报":
                s = crawl_rmrb(src, target_date=target_date)
            elif name == "中国化工报":
                s = crawl_ccin(src)
            else:
                s = crawl_generic(src)
            for k in ("fetched", "added", "skipped", "blocked",
                       "skipped_no_image", "skipped_category"):
                stats[k] = stats.get(k, 0) + s.get(k, 0)
        except Exception as e:
            err_msg = f"{name}: {type(e).__name__}: {e}"
            print(f"[crawl_all] 媒体源出错，跳过：{err_msg}")
            stats["errors"].append(err_msg)
    return stats


def crawl_date_range(start_date: str | date, end_date: str | date) -> dict:
    """按日期范围爬取所有媒体源的图片新闻（用于历史回填）。

    start_date / end_date: "YYYY-MM-DD" 字符串或 date 对象。
    - 中国石油报/辽宁日报/人民日报：逐日按指定日期的数字报 URL 抓取
    - 中国化工报：抓新闻列表，按文章发布日期过滤到范围内

    用法：crawler.crawl_date_range("2026-09-20", "2026-10-03")
    """
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    total = {"sources": 0, "fetched": 0, "added": 0, "skipped": 0, "blocked": 0,
             "skipped_no_image": 0, "skipped_category": 0, "errors": []}
    total_days = (end_date - start_date).days + 1
    print(f"\n{'='*60}")
    print(f"日期范围爬取：{start_date} ~ {end_date}（共 {total_days} 天）")
    print(f"{'='*60}")

    # 数字报：逐日抓取
    cur = start_date
    while cur <= end_date:
        print(f"\n>>> 日期 {cur.isoformat()} <<<")
        day_stats = crawl_all(target_date=cur)
        for k in ("fetched", "added", "skipped", "blocked",
                   "skipped_no_image", "skipped_category"):
            total[k] += day_stats.get(k, 0)
        total["errors"].extend(day_stats.get("errors", []))
        cur += timedelta(days=1)
        time.sleep(2)  # 日期间隔，避免请求过于密集

    # 中国化工报：新闻站无日期 URL，抓列表后按日期范围过滤
    print(f"\n>>> 中国化工报（按日期范围 {start_date}~{end_date} 过滤）<<<")
    for src in config.MEDIA_SOURCES:
        if src["name"] == "中国化工报":
            s = crawl_ccin(src, date_range=(start_date, end_date))
            for k in ("fetched", "added", "skipped", "blocked",
                       "skipped_no_image", "skipped_category"):
                total[k] += s.get(k, 0)
            total["errors"].extend(s.get("errors", []))
            break

    print(f"\n{'='*60}")
    print(f"日期范围爬取完成：{start_date} ~ {end_date}")
    print(f"  新增 {total['added']} 条，跳过 {total['skipped']} 条，"
          f"无图过滤 {total.get('skipped_no_image', 0)} 条，"
          f"分类过滤 {total.get('skipped_category', 0)} 条")
    if total["errors"]:
        print(f"  错误 {len(total['errors'])} 条")
    print(f"{'='*60}")
    return total


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
          f"新增 {stats['added']}，跳过(已存在) {stats['skipped']}，"
          f"无图跳过 {stats.get('skipped_no_image', 0)}，"
          f"非5类跳过 {stats.get('skipped_category', 0)}，"
          f"robots拦截 {stats['blocked']}")


if __name__ == "__main__":
    main()
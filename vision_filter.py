"""AI 视觉图片分类：用智谱免费视觉模型 glm-4v-flash 判断新闻图片题材，拦截无关图。

解决（2026-10-25）：人民日报「视觉」/辽宁日报等图片版，领导会见、漫画、旅游等
无关照片会混进"石油化工相关"的图片稿。文本闸门（标题/图注关键词）看不出图，
只能"看图"才能判断——这是给通过三道文本闸门的图片稿再加的第四道"视觉闸门"。

判定口径（给模型的标签清单，不是硬规则）：
  保留类 = 石油化工生产现场 / 设备装置 / 科技创新实验 / 安全生产演练 / 工程施工 / 能源保供
  拒绝类 = 领导会见 / 考察调研合影 / 会议座谈特写 / 漫画插画 / 旅游风景 /
           体育娱乐 / 美食生活 / 其他非工业场景

设计要点（与 ai_filter 同风格，保证可靠且绝不误杀）：
- 判定按 URL 缓存进 DB（vision_verdict 表），同一张图第二次起不重复调用视觉 API。
- 保守策略：一篇稿里抽查的图全部明确判"拒绝"才排除；任一图判"保留"或"拿不准"→ 保留。
- 失败安全（fail-open）：API 失败 / 熔断 / 未配 Key → 返回"不拒绝"，绝不因视觉故障误杀丢稿。
- 连续失败熔断：连续 VISION_CIRCUIT_BREAKER 次失败后跳过视觉调用，避免 API 故障时
  大量无效调用拖慢爬虫；成功一次自动重置。
- 输入用 400px 缩略图 base64，单次调用 token 极小（实测 ~280 tokens），免费档成本近零。
- 被判排除的稿进「已排除」列表（可人工一键恢复），不静默丢弃。
"""
from __future__ import annotations

import base64
import io
import json
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
import db

try:
    from PIL import Image
    from PIL import ImageOps

    _PIL_OK = True
except ImportError:  # pragma: no cover - 环境缺 Pillow 时优雅降级为不判
    _PIL_OK = False

logger = logging.getLogger(__name__)

# 给模型的标签清单（判断依据，非硬规则）
ALLOWED_LABELS = (
    "石油化工生产现场", "设备装置", "科技创新实验",
    "安全生产演练", "工程施工", "能源保供",
)
REJECT_LABELS = (
    "领导会见", "考察调研合影", "会议座谈特写", "漫画插画",
    "旅游风景", "体育娱乐", "美食生活", "其他非工业场景",
)

SYSTEM_PROMPT = (
    "你是新闻图片题材分类器。判断一张新闻照片属于哪一类，只输出 JSON："
    '{"label":"类别名"}。'
    "保留类=石油化工生产现场/设备装置/科技创新实验/安全生产演练/工程施工/能源保供。"
    "拒绝类=领导会见/考察调研合影/会议座谈特写/漫画插画/旅游风景/体育娱乐/美食生活/其他非工业场景。"
    "选最贴切的一个。不要输出 JSON 以外的文字。"
)

# 连续失败熔断阈值（读 config，默认 5）
FAIL_CIRCUIT_BREAKER = 5
_consecutive_failures = 0

_session: requests.Session | None = None


def _make_session() -> requests.Session:
    """带重试的 Session：视觉 API 网络抖动时自动重试。"""
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _session_() -> requests.Session:
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def is_enabled_for(source: str) -> bool:
    """该来源是否启用视觉闸门。config.VISION_SOURCES 为空集合 = 全部启用。"""
    if not getattr(config, "VISION_FILTER_ENABLED", False):
        return False
    sources = getattr(config, "VISION_SOURCES", set())
    if not sources:
        return True
    return source in sources


def _extract_json(text: str) -> str | None:
    """用括号配对栈提取首个完整 JSON 对象，避免贪婪正则误匹配。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _image_to_thumb_b64(raw: bytes) -> str | None:
    """原始图字节 → 400px 缩略图 base64 data URI。失败返回 None（不判，放行）。"""
    if not _PIL_OK or not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    try:
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((400, 400), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _fetch_image_bytes(url: str) -> bytes | None:
    """下载原图字节（复用 image_archive 的下载，含 cnpc Referer 处理）。"""
    try:
        from image_archive import _download
        return _download(url, timeout=20)
    except Exception:
        return None


def _decide_on_failure() -> None:
    """视觉 API 失败：计数 + 达阈值熔断告警。"""
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures == FAIL_CIRCUIT_BREAKER:
        logger.warning(
            f"[vision_filter] 连续失败 {_consecutive_failures} 次，视觉闸门熔断已打开，"
            "后续图片将按『不拒绝』处理（fail-open，不误杀），爬虫继续。"
        )


def _reset_failures() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def _call_vision(url: str, image_b64: str) -> str | None:
    """调智谱 glm-4v-flash 判一张图，返回类别 label；API 失败返回 None。"""
    try:
        resp = _session_().post(
            config.ZHIPU_CHAT_URL,
            headers={
                "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": getattr(config, "VISION_MODEL", "glm-4v-flash"),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_b64}},
                            {"type": "text", "text": "判断这张新闻图片的类别。"},
                        ],
                    },
                ],
                "temperature": 0,
                "max_tokens": 40,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        # 强制按 UTF-8 解码：智谱响应头不带 charset，requests 的 resp.json() 会按
        # latin-1 解码 UTF-8，导致中文 label 变成乱码、永远匹配不上（实测踩坑）。
        try:
            data = json.loads(resp.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        # 兜底：部分免费视觉模型是"推理模型"，答案可能落在 reasoning_content 而非 content
        # （实测 glm-4.6v-flash 在 token 预算内会这样）。两者任一有标签都能判。
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        raw = _extract_json(content)
        if not raw:
            return None
        return json.loads(raw).get("label")
    except Exception:
        return None


def _label_to_verdict(text: str) -> str:
    """把模型返回的类别文本映射为 accept/reject。用"含词"判断而非精确匹配。

    实测模型有时会把整组类别一次返回（如"石油化工生产现场/设备装置/科技创新实验/…"），
    精确匹配太脆、会让闸门失效。规则：
      - 命中任一拒绝词 → reject（领导会见/漫画/旅游等）
      - 命中任一保留词 → accept
      - 都命中或都未命中（拿不准）→ accept（fail-open，保守放行，绝不误杀）
    """
    if any(r in text for r in REJECT_LABELS):
        return "reject"
    if any(a in text for a in ALLOWED_LABELS):
        return "accept"
    return "accept"


def _classify_one(url: str) -> str | None:
    """判单张图。返回 'accept' / 'reject'；失败或拿不准返回 None（保守放行）。

    判定结果按 URL 缓存进 DB：同图第二次起不调 API。
    """
    if not url:
        return None
    if config.ZHIPU_API_KEY and _consecutive_failures >= FAIL_CIRCUIT_BREAKER:
        return None  # 熔断：跳过调用，放行
    # 先查缓存（缓存是判定结论的唯一权威，避免重复调用）
    try:
        cached = db.get_vision_verdict(url)
    except Exception:
        cached = None
    if cached in ("accept", "reject"):
        return cached

    raw = _fetch_image_bytes(url)
    image_b64 = _image_to_thumb_b64(raw) if raw else None
    if not image_b64:
        return None  # 拿不到图 → 拿不准 → 放行

    label = _call_vision(url, image_b64)
    if not label:
        _decide_on_failure()
        return None
    _reset_failures()

    verdict = _label_to_verdict(label)
    try:
        db.save_vision_verdict(url=url, verdict=verdict, reason=label, model=config.VISION_MODEL)
    except Exception:
        pass  # 缓存写失败不影响本次判定
    return verdict


def _classify_article_images(image_urls) -> tuple[bool, str]:
    """判一篇图片稿：全部抽查图都明确拒绝才排除，否则保留。返回 (是否拒绝, 依据)。"""
    if not config.ZHIPU_API_KEY:
        return False, ""  # 未配 Key：视觉不启用（fail-open）
    try:
        urls = json.loads(image_urls) if image_urls else []
    except (json.JSONDecodeError, TypeError):
        urls = []
    if not urls:
        return False, ""
    checked = []
    for u in urls[: getattr(config, "VISION_MAX_IMAGES_PER_ARTICLE", 2)]:
        v = _classify_one(u)
        if v is None:
            return False, ""  # 任一图拿不准/失败 → 保留整稿，绝不误杀
        checked.append((u, v))
    if not checked:
        return False, ""
    if all(v == "reject" for _, v in checked):
        labels = " / ".join(REJECT_LABELS)
        return True, f"视觉判定 {len(checked)} 张图均为无关题材（{labels}）"
    return False, ""


def make_vision_check():
    """返回一个视觉闸门回调，签名与 comic_check 一致：(image_urls)->(是否拒绝, 依据)。"""
    def _check(image_urls):
        try:
            return _classify_article_images(image_urls)
        except Exception as e:  # pragma: no cover - 兜底
            logger.warning(f"[vision_filter] 视觉判定异常（放行）：{e}")
            return False, ""
    return _check

"""全局配置：媒体源、栏目、爬取节流、环境变量读取。

2026-09-29 改造（方案 A 上云版）：
- 数据库从本地 SQLite 改为 Supabase PostgreSQL，连接串从环境变量读
- 语义嵌入从本地 bge 改为 Silicon Flow 免费 API（BAAI/bge-large-zh-v1.5）
- 路径相关字段（BASE_DIR / DATA_DIR / DB_PATH）保留但弃用，云端不用本地文件

2026-10-03 新增：AUTH_ENABLED 访问密码开关（默认开启，本地调试可设 AUTH_ENABLED=0 关闭）。

栏目 URL 说明（2026-09-29 已核实并填入真实地址）：
- 中国石油报：数字报是 SPA 单页应用，根入口 http://epaper.cnpc.com.cn/zgsyb/
  会自动 redirect 到当期 /zgsyb/YYYY-MM/DD/。crawler 抓当期 SPA 页后，从内联
  epaperObject JSON 里按版面 alias 名（要闻/炼化新材料/党的建设/班组天地）过滤。
  所有栏目 url 都填同一个根入口即可，crawler 只抓一次 SPA 页再按 alias 分发。
- 辽宁日报：服务端渲染，url 是版面列表页模板，含 {yyyymm} {dd} 占位符，
  crawler 跑时用当期日期填充（第02版=要闻；第05版=各地，含锦州新闻）。
"""
import os

from dotenv import load_dotenv

# 本地开发：从 .env 读环境变量；云端：Streamlit Cloud / GitHub Actions 用 secrets 注入
load_dotenv()

# ---------- 访问密码开关 ----------
# 默认开启（AUTH_ENABLED=1）；本地调试可在 .env 里设 AUTH_ENABLED=0 关闭登录门控。
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "1") == "1"

# ---------- 数据库连接（Supabase PostgreSQL 直连串） ----------
# 格式：postgresql://postgres.<ref>:<password>@aws-<region>.pooler.supabase.com:6543/postgres
DB_DSN = os.environ.get("DATABASE_URL", "")

# ---------- 智谱 Zhipu 免费 LLM (chat) API ----------
# 2026-10-02 新增：写稿与 AI 初选默认改走智谱 GLM 免费模型（OpenAI 兼容接口）。
# 注册 https://bigmodel.cn 后在「API 密钥」新建，免费模型永久可用。
# 模型名：优先 GLM-4.7-Flash；如已下线/变更，以智谱官方「免费模型」列表为准（改 ZHIPU_CHAT_MODEL 即可）。
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_CHAT_MODEL = os.environ.get("ZHIPU_CHAT_MODEL", "GLM-4.7-Flash")

# ---------- Silicon Flow 免费 embedding API ----------
# 注册 https://siliconflow.cn 后在「账号 → API 密钥」新建，免费送 14 元 ≈ 1.5 亿次嵌入调用
SF_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
SF_EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"
SF_EMBED_MODEL = "BAAI/bge-large-zh-v1.5"  # 升级到 large，比本地 base 更准
SF_EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度（base 是 768）

# ---------- Silicon Flow 免费 LLM (chat) API（保留作智谱失败时的后备） ----------
# 复用同一个 SF_API_KEY；chat 用于 AI 初选（爬虫入库前判断相关性，过滤无关稿）
SF_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SF_CHAT_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # 免费，中文表现好，判断相关性够用
# AI 初选开关：默认开；环境变量 AI_FILTER_ENABLED=0 可临时关（如调试爬虫时）
AI_FILTER_ENABLED = os.environ.get("AI_FILTER_ENABLED", "1") == "1"

# ---------- 兼容老代码的路径字段（云端弃用，保留避免 import 报错） ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = DATA_DIR  # 仅用于 app.py 侧栏文案展示，不再真实使用

# ---------- 持久化日志 ----------
import logging
from logging.handlers import TimedRotatingFileHandler

def setup_logging():
    """配置持久化日志：控制台 + 文件（按天滚动，保留30天）。

    云端（Streamlit Cloud）文件系统只读时，自动回退为仅控制台输出，
    避免因无法写日志文件导致应用启动崩溃。
    """
    log_dir = os.path.join(DATA_DIR, "logs")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # 避免重复添加 handler
    has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, TimedRotatingFileHandler)
                     for h in root.handlers)
    if not has_stream:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
    # 尝试写文件日志；只读文件系统（云端）时静默回退
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "app.log")
        has_file = any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers)
        if not has_file:
            fh = TimedRotatingFileHandler(log_file, when="D", interval=1, backupCount=30, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
    except (OSError, PermissionError):
        pass  # 云端只读文件系统：仅用控制台日志

setup_logging()
logger = logging.getLogger("dpetro")

# ---------- 爬虫节流 ----------
CRAWL_INTERVAL_SECONDS = 5  # 同一站点内两次请求之间的最小间隔
CRAWL_MAX_PER_COLUMN = 20  # 每个栏目最多抓多少条新稿（一次运行）
REQUEST_TIMEOUT = 15  # 请求超时（秒）

# UA：自报家门，便于媒体侧联系；请按实际情况改成你的姓名/邮箱
# 本地 .env 没设就用默认值
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# 是否在数据库为空时自动灌入演示数据（便于先看到 UI）
# 真实爬取已跑通后改为 False（2026-09-29）
DEMO_SEED_ON_EMPTY = False

# ---------- 5 类图片新闻分类映射 ----------
# 2026-10-04 调整：按用户需求明确为 5 类题材
# 1.炼油化工新材料 2.工业生产 3.科技创新 4.人工智能 5.安全生产
# 爬虫入库前强制过滤：只有命中以下 5 类之一的图片新闻才入库。
# CATEGORY_MAP：按「源/栏目」精确映射（零 API 成本，主路径）
# CATEGORY_KEYWORDS：栏目未命中时，按标题关键词兜底分类
CATEGORY_MAP = {
    "炼油化工新材料": {
        "中国石油报/炼化新材料",
        "中国化工报/国内化工",
        "中国化工报/石化",
        "人民日报/绿色",      # 绿色低碳归入炼油化工新材料（石化环保）
        "人民日报/生态",
        "中国石油报/环保",
        "中国化工报/节能",
    },
    "工业生产": {
        "中国石油报/要闻",
        "中国石油报/班组天地",
        "辽宁日报/要闻",
        "辽宁日报/各地",
        "人民日报/要闻",
        "人民日报/视觉",
        "中国化工报/要闻",
    },
    "科技创新": {
        "人民日报/科技",
        "中国石油报/科技",
        "中国石油报/创新",
        "中国化工报/科技",
        "中国化工报/创新",
    },
    # 人工智能、安全生产主要靠标题关键词兜底匹配（无固定报纸栏目）
}
# 扁平化：所有允许入库的「源/栏目」集合
ALLOWED_SOURCE_COLUMNS = {sc for cats in CATEGORY_MAP.values() for sc in cats}

# 标题关键词兜底：栏目不在 CATEGORY_MAP 时，按标题关键词判定类别
CATEGORY_KEYWORDS = {
    "炼油化工新材料": ["催化", "重整", "加氢", "裂解", "炼油", "石化", "炼化", "乙烯", "芳烃", "油品", "汽油", "柴油", "装置", "储罐", "油气", "钻井", "采油", "天然气", "新材料", "化工", "聚丙烯", "聚乙烯", "树脂", "纤维", "橡胶"],
    "工业生产": ["生产", "开工", "投产", "达产", "产量", "产能", "检修", "开车", "运行", "作业", "施工", "车间", "产线", "流水线", "制造", "加工", "装配"],
    "科技创新": ["科技", "技术", "研发", "创新", "专利", "攻关", "突破", "首创", "首个", "小改小革", "技改", "成果", "鉴定", "发明", "实验", "试验"],
    "人工智能": ["人工智能", "AI", "大模型", "算法", "机器学习", "深度学习", "神经网络", "GPT", "算力", "智能体", "AIGC", "计算机视觉", "自然语言", "具身智能", "机器人"],
    "安全生产": ["安全", "事故", "隐患", "应急", "救援", "消防", "演练", "隐患排查", "安全生产", "风险", "防控", "监督", "检查", "环保", "节能", "减排", "治理"],
}

# 媒体源与栏目配置
# priority: P0=每日必扫；P1=有空就扫
MEDIA_SOURCES = [
    {
        "name": "中国石油报",
        # 数字报 SPA 入口；访问根 URL 会自动 redirect 到当期 /zgsyb/YYYY-MM/DD/
        "home": "http://epaper.cnpc.com.cn/zgsyb/",
        "columns": [
            # 所有栏目 url 都填根入口，crawler 抓一次当期 SPA 页后按版面 alias 名过滤分发。
            # alias 名（要闻/炼化新材料/党的建设/班组天地）来自当期 epaperObject 的 page_XXX.alias 字段。
            # 注：第03版 alias 随期会变（炼化新材料/油气新能源/理论与实践/党的建设）。
            # aliases 字段列候选：当期 alias 命中任一候选，文章就归到该栏目（name 用于查 db.column_id）。
            {"name": "要闻", "url": "http://epaper.cnpc.com.cn/zgsyb/", "priority": "P0", "category": "工业生产"},
            {"name": "炼化新材料", "url": "http://epaper.cnpc.com.cn/zgsyb/", "priority": "P0",
             "aliases": ["炼化新材料", "油气新能源"], "category": "炼油化工新材料"},
            # 党的建设不在 5 类图片新闻范围内，暂不抓取
            # {"name": "党的建设", "url": "http://epaper.cnpc.com.cn/zgsyb/", "priority": "P0"},
            {"name": "班组天地", "url": "http://epaper.cnpc.com.cn/zgsyb/", "priority": "P1", "category": "工业生产"},
        ],
    },
    {
        "name": "辽宁日报",
        "home": "https://epaper.lnd.com.cn/lnrbepaper/pc/",
        "columns": [
            # url 是版面列表页模板，{yyyymm} {dd} 由 crawler 跑时用当期日期填充。
            # 第02版=要闻（稳定）；第05版=各地（含锦州新闻，版名随期会变但版号稳定）。
            # 若 404（当期未出），crawler 自动回退试前 1-2 天。
            {"name": "要闻", "url": "https://epaper.lnd.com.cn/lnrbepaper/pc/layout/{yyyymm}/{dd}/node_02.html", "priority": "P0", "category": "工业生产"},
            {"name": "各地", "url": "https://epaper.lnd.com.cn/lnrbepaper/pc/layout/{yyyymm}/{dd}/node_05.html", "priority": "P1", "category": "工业生产"},
        ],
    },
    {
        "name": "人民日报",
        "home": "http://paper.people.com.cn/rmrb/pc/",
        "columns": [
            # 人民日报版号随期变，crawler 动态抓当期版面列表，按版名映射到 category。
            # 这里列出版名 → category 映射（version 字段为版号占位，实际由 crawler 解析）。
            {"name": "要闻", "url": "http://paper.people.com.cn/rmrb/pc/layout/{yyyymm}/{dd}/node_01.html", "priority": "P0", "category": "工业生产"},
            {"name": "视觉", "url": "http://paper.people.com.cn/rmrb/pc/layout/{yyyymm}/{dd}/node_04.html", "priority": "P0", "category": "工业生产"},
            {"name": "绿色", "url": "http://paper.people.com.cn/rmrb/pc/layout/{yyyymm}/{dd}/node_05.html", "priority": "P0", "category": "炼油化工新材料"},
            # 科技版号随期变，crawl_rmrb 会按版名"科技"动态匹配
        ],
    },
    {
        "name": "中国化工报",
        # ccin.com.cn 的 SSL 证书与主机名不匹配，浏览器/爬虫会报证书警告；
        # 该站 HTTP 可正常访问且不重定向到 HTTPS，统一用 http 彻底规避 SSL 问题。
        "home": "http://www.ccin.com.cn/",
        "columns": [
            # 中化新网新闻列表页，每篇配缩略图
            {"name": "国内化工", "url": "http://www.ccin.com.cn/c/index_domestic", "priority": "P0", "category": "炼油化工新材料"},
            {"name": "科技", "url": "http://www.ccin.com.cn/c/index_tech", "priority": "P0", "category": "科技创新"},
            {"name": "节能", "url": "http://www.ccin.com.cn/c/key_saving", "priority": "P0", "category": "炼油化工新材料"},
        ],
    },
]

# 人民日报版名 → 5 类映射（版号随期变，按版名匹配更稳）
RMRB_EDITION_CATEGORY = {
    "要闻": "工业生产",
    "视觉": "工业生产",
    "绿色": "炼油化工新材料",
    "生态": "炼油化工新材料",
    "科技": "科技创新",
    "创新": "科技创新",
    "经济": "工业生产",
}

# 常规选题日历种子（用于 calendar_engine 模块占位演示）
ROUTINE_TOPICS_SEED = [
    {"name": "春季检修（春检）", "start_month": 3, "start_day": 1, "end_month": 5, "end_day": 15,
     "recommended_column": "安全生产", "lead_days": 21,
     "note": "锦州石化常在3-4月停工检修，提前3周开始追现场图片+检修节点稿件"},
    {"name": "安全生产月", "start_month": 6, "start_day": 1, "end_month": 6, "end_day": 30,
     "recommended_column": "安全生产", "lead_days": 14,
     "note": "6月全国安全生产月，配合隐患排查/应急演练选题"},
    {"name": "七一党建", "start_month": 6, "start_day": 20, "end_month": 7, "end_day": 5,
     "recommended_column": "党建", "lead_days": 21,
     "note": "党员先锋岗/主题党日/红色教育基地素材"},
    {"name": "迎峰度夏", "start_month": 6, "start_day": 1, "end_month": 9, "end_day": 30,
     "recommended_column": "产经视线", "lead_days": 14,
     "note": "夏季高温下装置平稳运行、油品保供"},
    {"name": "冬季保供", "start_month": 10, "start_day": 15, "end_month": 3, "end_day": 15,
     "recommended_column": "产经视线", "lead_days": 30,
     "note": "储气库注采、成品油冬季保供、极端天气应对"},
    {"name": "VOCs治理/环保", "start_month": 4, "start_day": 1, "end_month": 10, "end_day": 31,
     "recommended_column": "炼化新材料", "lead_days": 14,
     "note": "环保设施运行、LDAR检测、超低排放改造进度"},
]


def ensure_dirs():
    """创建本地目录（图片上传等需要）。

    云端（Streamlit Cloud）文件系统只读时静默跳过，不影响核心功能。
    图片上传功能在云端会受限（不持久），但审核/对标/撰稿不受影响。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except (OSError, PermissionError):
        pass
    return

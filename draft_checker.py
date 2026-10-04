"""成稿体检器：投稿前对草稿做四项质量检查 + 三版适配建议。

MVP 实现：基于正则的轻量规则，零依赖、零外部 API。
"""
from __future__ import annotations

import re
import json
import requests as _requests

import config as _config_zhipu


def _extract_json_array(text: str) -> list | None:
    """用括号配对栈提取首个完整 JSON 数组，避免贪婪正则误匹配。"""
    start = text.find("[")
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
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None

# 模糊时间表达：不含具体日期/月份
FUZZY_TIME = re.compile(r"(最近|近日|日前|前不久|前段|不久前|这些天|这阵子|近期|近来|目前|眼下|时下|近期以来|最近一段|这段时间)")
# 空泛数据：缺少具体数字的「大幅/显著/稳步」等
VAGUE_DATA = re.compile(r"(大幅|显著|明显|稳步|大幅提升|迈上新台阶|创历史新高|再创新高|稳步增长|连续提升|突破性进展|跃升新台阶|成效显著|成效明显)")
# 绝对化用词
ABSOLUTE = re.compile(r"(完全|全部|首创|国内第一|国际领先|世界一流|绝对|率先|唯一|最大|第一|顶尖|领先|首次|独家|首屈一指|独一无二|史无前例|空前)")
# 图片说明常见缺陷：只说"现场""工作场景"等空泛说明
WEAK_CAPTION = re.compile(r"(图[为系作](现场|工作场景|施工场景|施工|工作|操作|场景))")

# 常见错别字字典（高频、无歧义，可按反馈扩充）
COMMON_TYPOS = {
    "按装": "安装", "哀声叹气": "唉声叹气", "百费待兴": "百废待兴",
    "帐号": "账号", "帐目": "账目", "帐户": "账户", "记帐": "记账",
    "做为": "作为", "既使": "即使", "偶而": "偶尔", "幅射": "辐射",
    "急燥": "急躁", "暴燥": "暴躁", "干躁": "干燥", "枯躁": "枯燥", "焦燥": "焦躁",
    "震憾": "震撼", "引伸": "引申", "申张": "伸张", "欠收": "歉收",
    "玩固": "顽固", "完强": "顽强", "烦杂": "繁杂", "兰色": "蓝色", "兰天": "蓝天",
    "不记其数": "不计其数", "走头无路": "走投无路", "责无旁代": "责无旁贷",
    "变本加利": "变本加厉", "穿流不息": "川流不息", "一股作气": "一鼓作气",
    "搏取": "博取", "搏弈": "博弈", "明辩是非": "明辨是非", "辩识": "辨识",
    "鬼鬼崇崇": "鬼鬼祟祟", "纷至踏来": "纷至沓来", "换然一新": "焕然一新",
    "容光唤发": "容光焕发",
}


def _check_lead(text: str) -> list:
    """检查导语（首段）是否含新闻要素：具体时间、长度合理。"""
    issues = []
    if not text:
        return issues
    first_para = text.split("\n")[0][:200]
    if not re.search(r"\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\d{1,2}日", first_para):
        issues.append("导语缺具体时间：首段无'X月X日'格式，建议补具体日期。")
    if len(first_para) < 30:
        issues.append(f"导语过短（{len(first_para)}字）：建议补足新闻要素（时间/地点/人物/事件/原因）。")
    elif len(first_para) > 200:
        issues.append(f"导语过长（{len(first_para)}字）：建议精简到 80-120 字。")
    return issues


def _check_sentence_quality(text: str) -> list:
    """检查句子质量：超长句（>80字）、逗号连用（≥5个）、重复用词（≥3次）。"""
    issues = []
    if not text:
        return issues
    sentences = re.split(r"[。！？\n]", text)
    for i, s in enumerate(sentences, 1):
        s = s.strip()
        if not s:
            continue
        if len(s) > 80:
            issues.append(f"超长句（{len(s)}字，第{i}句）：建议拆分，单句不超 80 字。")
        if s.count("，") >= 5:
            issues.append(f"逗号连用（{s.count(chr(0xFF0C))}个，第{i}句）：建议断句。")
    for p_idx, p in enumerate(text.split("\n"), 1):
        if len(p) < 30:
            continue
        words = re.findall(r"[\u4e00-\u9fa5]{3,4}", p)
        wc: dict = {}
        for w in words:
            wc[w] = wc.get(w, 0) + 1
        for w, c in wc.items():
            if c >= 3:
                issues.append(f"重复用词「{w}」（第{p_idx}段出现{c}次）：建议替换或精简。")
    return issues[:5]


# 三版适配规则（基于锦州石化投稿经验，可按反馈调整）
VERSION_RULES = {
    "辽报版": {
        "去企业内宣词": ["党员先锋岗", "党建+生产", "内操外操", "装置一次开车成功"],
        "提示": "辽报偏好'区域/民生视角'，企业术语需解释，标题忌行业黑话。",
    },
    "中石油版": {
        "保留行业术语": ["催化裂化", "连续重整", "加氢裂化", "VOCs", "LDAR", "国ⅥB"],
        "提示": "中石油报可保留'春检/储气库注采'等术语，重点在节点数据与对比。",
    },
    "企业内网版": {
        "可保留": ["党员先锋岗", "临时党支部", "小改小革", "装置一次开车成功"],
        "提示": "企业内网可保留内宣口径，但需把人物姓名班组写全。",
    },
}


def check(draft_title: str, draft_text: str, caption: str = "") -> dict:
    """返回 {issues:[...], versions:{辽报版/中石油版/企业内网版: [建议]}}。"""
    blob = f"{draft_title}\n{draft_text}\n{caption}"
    issues: list[str] = []

    for m in FUZZY_TIME.finditer(blob):
        issues.append(f"模糊时间「{m.group(1)}」：建议换成具体日期或'X月X日'。")
    for m in VAGUE_DATA.finditer(blob):
        issues.append(f"空泛数据「{m.group(1)}」：补一个对比数字（如同比+X%、X万吨）。")
    for m in ABSOLUTE.finditer(blob):
        issues.append(f"绝对化用词「{m.group(1)}」：除非有权威认定，否则改成'稳步走在前列'之类表述。")
    if caption and WEAK_CAPTION.search(caption):
        issues.append(f"图片说明空泛：'{caption}' 建议补人物+装置+动作（如'催化主操张三在调整反应温度'）。")
    # 常见错别字
    for wrong, right in COMMON_TYPOS.items():
        if wrong in blob:
            issues.append(f"疑似错别字「{wrong}」：建议改为「{right}」。")
    # 导语 5W1H 检查
    issues.extend(_check_lead(draft_text))
    # 句子质量检查
    issues.extend(_check_sentence_quality(draft_text))

    versions: dict[str, list[str]] = {}
    for ver, rule in VERSION_RULES.items():
        advice: list[str] = [rule["提示"]]
        for kw in rule.get("去企业内宣词", []):
            if kw in blob:
                advice.append(f"需删/替换企业内宣词：「{kw}」")
        for kw in rule.get("保留行业术语", []) + rule.get("可保留", []):
            if kw not in blob and ver == "中石油版":
                advice.append(f"建议保留行业术语：「{kw}」（如有相关内容）")
        versions[ver] = advice
    return {"issues": issues or ["未发现明显问题"], "versions": versions}

_TC_BASE = _config_zhipu.TENCENTCLOUD_CHAT_URL
_MODEL_PROOFREAD = _config_zhipu.TENCENTCLOUD_CHAT_MODEL


def ai_proofread(draft_title: str, draft_text: str, caption: str = "") -> dict:
    """AI 深度校对：错别字、标点、语病、新闻规范、数字单位、敏感表述。

    智谱 GLM 优先（免费），失败回退腾讯云 deepseek。
    """
    sys_prompt = (
        "你是一位资深的中国石油石化行业新闻出版校对编辑。请对稿件进行校对，找出确实存在的问题："
        "1. 错别字、多字漏字（注意'的/得/地'、'帐/账'、'作/做'、'安装'误为'按装'等易错词）"
        "2. 标点符号错误（中英文标点混用、引号括号配对、逗号句号误用）"
        "3. 语法语病、语句不通顺、超长句（>80字）"
        "4. 新闻写作规范：导语是否含5W1H要素、结构是否倒金字塔、段落是否过长"
        "5. 数字、单位、日期格式不统一（中文数字与阿拉伯数字混用、单位规范）"
        "6. 石油石化行业术语规范（装置名、工艺名、单位是否准确）"
        "7. 敏感表述或政治表述不当（涉台涉港澳、领导人姓名职务、统计数据口径）"
        "只列出确实存在的问题，不要无中生有，不要提风格建议。"
        "按问题类型分类返回 JSON："
        "{\"错别字\":[\"...\"],\"标点\":[\"...\"],\"语法\":[\"...\"],"
        "\"新闻规范\":[\"...\"],\"数字单位\":[\"...\"],\"行业术语\":[\"...\"],"
        "\"敏感表述\":[\"...\"]}"
        "无问题的类别返回空数组。只返回 JSON，不要其他文字。"
    )
    user_msg = f"标题：{draft_title}\n\n正文：\n{draft_text}\n"
    if caption:
        user_msg += f"\n图片说明：{caption}\n"
    user_msg += "\n请按 system prompt 要求的分类 JSON 返回，无问题的类别返回空数组。"

    def _call(base: str, api_key: str, model: str):
        try:
            resp = _requests.post(
                base,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1024,
                    "thinking": {"type": "disabled"},
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _parse(data: dict):
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        obj = _extract_json_object(content)
        if isinstance(obj, dict):
            categorized = {}
            for k, v in obj.items():
                if isinstance(v, list):
                    categorized[k] = [str(x) for x in v]
                else:
                    categorized[k] = [str(v)] if v else []
            return categorized
        arr = _extract_json_array(content)
        if arr is not None:
            return {"其他": arr if arr else ["AI 校对未发现问题"]}
        return {"其他": [content] if content else ["AI 校对未发现问题"]}

    # 1) 主力：智谱 GLM（免费）
    zp_key = _config_zhipu.ZHIPU_API_KEY
    if zp_key:
        data = _call(_ZHIPU_BASE, zp_key, _ZHIPU_MODEL)
        if data:
            parsed = _parse(data)
            if parsed is not None:
                return {"ai_issues": parsed, "ok": True}

    # 2) 后备：腾讯云 deepseek
    tc_key = _config_zhipu.TENCENTCLOUD_API_KEY
    if not tc_key:
        return {"ai_issues": ["智谱与腾讯云 Key 均未配置，跳过 AI 校对"], "ok": False}
    data = _call(_TC_BASE, tc_key, _MODEL_PROOFREAD)
    if not data:
        return {"ai_issues": ["AI 校对请求失败（智谱与腾讯云均不可用）"], "ok": False}
    parsed = _parse(data)
    if parsed is not None:
        return {"ai_issues": parsed, "ok": True}
    return {"ai_issues": ["AI 校对返回格式异常"], "ok": False}

# ===== 三版适配 LLM 量身建议（智谱→腾讯回退，启发式兜底）=====

_ZHIPU_BASE = _config_zhipu.ZHIPU_CHAT_URL
_ZHIPU_MODEL = _config_zhipu.ZHIPU_CHAT_MODEL

# 三版定位 system prompt（区分媒体属性，引导 LLM 给针对性建议）
_VERSION_PROFILES = {
    "辽报版": (
        "你是辽宁日报资深编辑。辽报偏好区域/民生视角，企业术语需解释，"
        "标题忌行业黑话。请基于稿件内容，给出针对性改写建议："
        "1) 哪些企业内宣词需删/替换为民生化表述；"
        "2) 哪些行业术语需补充解释或换成通俗说法；"
        "3) 标题如何改写更贴近区域读者；"
        "4) 导语如何突出区域/民生相关性。"
    ),
    "中石油版": (
        "你是中国石油报资深编辑。中石油报可保留行业术语，"
        "重点在节点数据与对比。请基于稿件内容，给出针对性改写建议："
        "1) 哪些节点数据/同比对比需补充或强化；"
        "2) 行业术语保留是否得当，是否需统一规范；"
        "3) 标题如何突出行业节点意义；"
        "4) 导语如何突出数据/对比亮点。"
    ),
    "企业内网版": (
        "你是锦州石化内网资深编辑。内网可保留内宣口径，"
        "但需把人物姓名班组写全，突出基层故事。请基于稿件内容，给出针对性改写建议："
        "1) 哪些人物/班组/装置名需写全（不能只写'某员工'）；"
        "2) 内宣表述是否到位（如党员先锋岗、临时党支部等可保留）；"
        "3) 标题如何突出本企业特色；"
        "4) 导语如何突出基层故事感。"
    ),
}


def _extract_json_object(text: str) -> dict | None:
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
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _version_advice_llm(draft_title: str, draft_text: str, caption: str, version: str) -> dict:
    """LLM 量身生成三版适配建议 + 导语示例。智谱→腾讯回退。"""
    profile = _VERSION_PROFILES.get(version, "")
    if not profile:
        return {"advice": [], "lead_example": "", "ok": False, "source": "llm"}

    sys_prompt = profile + (
        "\n\n请严格以 JSON 格式返回，字段："
        "\"advice\"（改写建议列表，每个元素是具体建议字符串，4-6 条），"
        "\"lead_example\"（改写后的导语示例，1 段约 80-120 字，体现该版风格）。"
        "只返回 JSON，不要其他文字。"
    )
    user_msg = f"稿件标题：{draft_title}\n\n稿件正文：\n{draft_text}\n"
    if caption:
        user_msg += f"\n图片说明：{caption}\n"

    def _call(base: str, api_key: str, model: str):
        try:
            resp = _requests.post(
                base,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.5,
                    "max_tokens": 1024,
                    "thinking": {"type": "disabled"},
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _parse(data: dict):
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return None
        obj = _extract_json_object(content)
        if not isinstance(obj, dict):
            return None
        advice = obj.get("advice", [])
        if not isinstance(advice, list):
            advice = []
        lead = obj.get("lead_example", "")
        if not isinstance(lead, str):
            lead = ""
        return {"advice": [str(a) for a in advice], "lead_example": lead}

    # 1) 主力：智谱 GLM（免费）
    zp_key = _config_zhipu.ZHIPU_API_KEY
    if zp_key:
        data = _call(_ZHIPU_BASE, zp_key, _ZHIPU_MODEL)
        if data:
            parsed = _parse(data)
            if parsed and parsed["advice"]:
                return {"advice": parsed["advice"], "lead_example": parsed["lead_example"],
                        "ok": True, "source": "llm"}

    # 2) 后备：腾讯云 deepseek
    _config_zhipu.logger.info(f"draft_checker: 智谱三版适配({version})失败或未配 Key，回退腾讯云")
    tc_key = _config_zhipu.TENCENTCLOUD_API_KEY
    if tc_key:
        data = _call(_TC_BASE, tc_key, _MODEL_PROOFREAD)
        if data:
            parsed = _parse(data)
            if parsed and parsed["advice"]:
                return {"advice": parsed["advice"], "lead_example": parsed["lead_example"],
                        "ok": True, "source": "llm"}

    return {"advice": [], "lead_example": "", "ok": False, "source": "llm"}


def _version_advice_heuristic(draft_title: str, draft_text: str, caption: str, version: str) -> dict:
    """启发式三版适配建议（兜底，零外部依赖）。"""
    blob = f"{draft_title}\n{draft_text}\n{caption}"
    rule = VERSION_RULES.get(version, {})
    advice: list[str] = [rule.get("提示", "")]

    if version == "辽报版":
        for kw in rule.get("去企业内宣词", []):
            if kw in blob:
                advice.append(
                    f"需删/替换企业内宣词「{kw}」：建议换成民生化/区域视角表述"
                    f"（如'党员先锋岗'→'在某岗位连续工作X年的老党员'）。"
                )
        for kw in ["催化裂化", "连续重整", "加氢裂化", "VOCs", "LDAR"]:
            if kw in blob:
                advice.append(
                    f"行业术语「{kw}」需补充解释或换成通俗说法"
                    f"（如'催化裂化'→'核心炼油装置'）。"
                )
        if not any(k in blob for k in ["锦州", "辽宁", "辽西"]):
            advice.append("建议在导语补区域锚点（如'锦州石化'/'辽西地区'），强化地域相关性。")
    elif version == "中石油版":
        if not re.search(r"\d+\.?\d*%|\d+万吨|\d+万|m³|吨", blob):
            advice.append("导语缺数据/对比：建议补'处理量X万吨/同比+X%'等节点数据。")
        for kw in rule.get("保留行业术语", []):
            if kw not in blob:
                advice.append(f"建议保留行业术语「{kw}」（如相关内容有）。")
    elif version == "企业内网版":
        if re.search(r"某员工|某班|某岗位", blob):
            advice.append("人物/班组未写全：'某员工'/'某班'需替换为真实姓名+班组+装置。")
        for kw in rule.get("可保留", []):
            if kw not in blob:
                advice.append(f"内宣表述「{kw}」可保留（如相关内容有）。")

    return {"advice": advice, "lead_example": "", "source": "heuristic"}


def version_advice(draft_title: str, draft_text: str, caption: str, version: str) -> dict:
    """三版适配主函数：LLM 优先，失败回退启发式。

    返回 {advice:[...], lead_example:str, source:"llm"/"heuristic"}。
    """
    r = _version_advice_llm(draft_title, draft_text, caption, version)
    if r["ok"] and r["advice"]:
        return r
    return _version_advice_heuristic(draft_title, draft_text, caption, version)



if __name__ == "__main__":
    r = check("春检攻坚圆满收官",
              "最近锦州石化春检顺利完成，催化裂化装置一次开车成功，党员先锋岗带头攻坚。",
              "图为现场工作场景")
    print(r)

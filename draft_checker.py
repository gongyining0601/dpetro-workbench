"""成稿体检器：投稿前对草稿做四项质量检查 + 三版适配建议。

MVP 实现：基于正则的轻量规则，零依赖、零外部 API。
"""
from __future__ import annotations

import re

# 模糊时间表达：不含具体日期/月份
FUZZY_TIME = re.compile(r"(最近|日前|前不久|前段|不久前|这些天|这阵子|近期)")
# 空泛数据：缺少具体数字的「大幅/显著/稳步」等
VAGUE_DATA = re.compile(r"(大幅|显著|明显|稳步|大幅提升|迈上新台阶)")
# 绝对化用词
ABSOLUTE = re.compile(r"(完全|全部|首创|国内第一|国际领先|世界一流|绝对)")
# 图片说明常见缺陷：只说"现场""工作场景"等空泛说明
WEAK_CAPTION = re.compile(r"(图[为系]现场|图[为系]工作场景|图[为系]施工场景)")

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

import os
import requests as _requests

_SF_BASE = "https://api.siliconflow.cn/v1/chat/completions"
_MODEL_PROOFREAD = "THUDM/glm-4-9b-chat"


def ai_proofread(draft_title: str, draft_text: str, caption: str = "") -> dict:
    """AI 深度校对：错别字、标点、语病、新闻规范、数字单位、敏感表述。"""
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        return {"ai_issues": ["未配置 SILICONFLOW_API_KEY，跳过 AI 校对"], "ok": False}

    sys_prompt = (
        "你是一位资深新闻出版校对编辑。请对稿件进行校对，找出以下问题："
        "1. 错别字、多字漏字"
        "2. 标点符号错误"
        "3. 语法语病、语句不通顺"
        "4. 新闻写作规范问题（导语缺失、结构混乱等）"
        "5. 数字、单位、日期格式不统一"
        "6. 敏感表述或政治表述不当"
        "只列出确实存在的问题，不要无中生有。"
    )
    user_msg = f"标题：{draft_title}\n\n正文：\n{draft_text}\n"
    if caption:
        user_msg += f"\n图片说明：{caption}\n"
    user_msg += "\n请以 JSON 数组格式返回问题列表，每个元素是一个问题描述字符串。如无问题返回空数组 []。"

    try:
        resp = _requests.post(
            _SF_BASE,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _MODEL_PROOFREAD,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.3,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        import re as _re
        m = _re.search(r"\[.*\]", content, _re.S)
        if m:
            import json as _json
            issues = _json.loads(m.group(0))
            return {"ai_issues": issues if issues else ["AI 校对未发现问题"], "ok": True}
        return {"ai_issues": [content] if content else ["AI 校对未发现问题"], "ok": True}
    except Exception as e:
        return {"ai_issues": [f"AI 校对失败：{e}"], "ok": False}

if __name__ == "__main__":
    r = check("春检攻坚圆满收官",
              "最近锦州石化春检顺利完成，催化裂化装置一次开车成功，党员先锋岗带头攻坚。",
              "图为现场工作场景")
    print(r)

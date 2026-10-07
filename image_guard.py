"""漫画/插画识别：判断一张图是手绘漫画还是新闻照片。

背景（2026-10-07）：辽宁日报等报纸会配漫画/插画，题材上不属于要保留的
"新闻图片"，但标题里往往看不出是漫画（标题不写"漫画"二字），只能看图。

原理（手机拍照/报纸扫描的漫画有很强的像素特征）：
  - 白底占比高：手绘漫画大面积留白；新闻照片（哪怕天空/雪地）很难超过一半
  - 画面颜色少：漫画用色少且平涂；照片色彩过渡丰富
阈值用真实样本校准过（2026-10-07，44 张库内真实新闻照片全部不被误判，
样本特征：白底占比最高 0.34、活跃色最少 47）。
判定保守：只有两条证据同时满足才算漫画，拿不准就放行（宁可漏放，
因为被拦的会进「已排除」列表，误杀可一键恢复）。
"""
from __future__ import annotations

import io

try:
    from PIL import Image

    _PIL_OK = True
except ImportError:  # pragma: no cover
    _PIL_OK = False

# 校准阈值（对照真实新闻照片样本留出的安全边距）
WHITE_RATIO_MIN = 0.50      # 照片样本最高 0.34（雪地/天空），漫画通常 >0.5
ACTIVE_COLORS_MAX = 24      # 照片样本最少 47/48，漫画平涂一般 <24
TOP3_RATIO_MIN = 0.60       # 前三种颜色占比：漫画大面积同色，照片色彩分散
LONG_LINE_MIN = 6           # 表格/图表有大量横平竖直的长直线（≥6 条，实测 6~10）；
                            # 手绘漫画只有分格边框（1~4 条）。校准样本见 scan_comics.py


def _count_long_lines(small) -> int:
    """数"横平竖直的长直线"条数：表格/图表的特征，漫画几乎没有。

    表格的每一行/列都有一条贯穿的长直线；照片即使有暗色带也不会笔直贯穿。
    """
    try:
        import numpy as np

        arr = np.asarray(small.convert("L"))
        dark = arr < 180   # 校准值：表格线在缩放后变灰，128 太严（表格漏检），180 稳定
        h, w = dark.shape
        count = 0
        # 水平长直线：某一行连续暗像素超过宽度的一半
        for row in dark:
            run = best = 0
            for v in row:
                run = run + 1 if v else 0
                best = max(best, run)
            if best > w * 0.5:
                count += 1
        # 垂直长直线：某一列连续暗像素超过高度的一半
        for col in dark.T:
            run = best = 0
            for v in col:
                run = run + 1 if v else 0
                best = max(best, run)
            if best > h * 0.5:
                count += 1
        return count
    except Exception:
        return 0


def looks_like_comic(image_bytes: bytes, sample_width: int = 220) -> tuple[bool, str]:
    """判断图片字节是不是漫画/插画。返回 (是否漫画, 依据说明)。"""
    if not _PIL_OK:
        return False, ""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return False, ""
    w = sample_width
    small = img.resize((w, max(1, int(w * img.height / img.width))))
    px = list(small.getdata())
    n = len(px)
    if not n:
        return False, ""
    white = sum(1 for p in px if min(p) > 232) / n
    q = small.quantize(colors=48)
    colors = q.getcolors(999999) or []
    active = sum(1 for c, _ in colors if c / n > 0.005)
    top3 = sum(c for c, _ in colors[:3]) / n
    if white >= WHITE_RATIO_MIN and (active <= ACTIVE_COLORS_MAX or top3 >= TOP3_RATIO_MIN):
        # 白底+少色还可能是数据表格/图表截图，用长直线数把它们排掉
        lines = _count_long_lines(small)
        if lines >= LONG_LINE_MIN:
            return False, ""   # 是表格/图表，不是漫画
        why = (f"白底占比 {white:.0%}、活跃颜色 {active} 种、前三种颜色占 {top3:.0%}、"
               f"长直线 {lines} 条——大片留白+用色少+无表格网格，更像手绘漫画")
        return True, why
    return False, ""

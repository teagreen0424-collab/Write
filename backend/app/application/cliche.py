"""去 AI 味扫描：纯字符串匹配，不调 LLM。

词库内置四类套路表达，命中项与一致性冲突字段同形，前端可复用高亮渲染。
"""
from typing import Dict, List

# 四类套路表达词库（约 100 条）
CLICHE_TERMS: Dict[str, List[str]] = {
    "万能副词": [
        "不禁", "仿佛", "竟然", "顿时", "瞬间", "缓缓", "微微", "淡淡", "默默", "轻轻",
        "深深", "不由自主", "情不自禁", "深吸一口气", "心底一沉", "说到底", "不由得",
    ],
    "套路动作": [
        "嘴角勾起", "勾唇一笑", "眉头微皱", "瞳孔骤缩", "倒吸一口凉气", "心中一凛",
        "心头一颤", "脸色一变", "眼神一暗", "眼中闪过一丝", "攥紧拳头", "指节泛白",
        "喉结滚动", "嘴角扬起", "目光灼灼", "呼吸一滞",
    ],
    "面部套话": [
        "嘴角泛起一抹", "眼底深处", "脸上浮现", "神色复杂", "意味深长", "嘴角的弧度",
        "眸色渐深", "神情莫测", "眼中精光一闪", "笑意不达眼底", "面无表情地看着",
    ],
    "心理填充": [
        "五味杂陈", "百感交集", "思绪万千", "心中一紧", "说不出的滋味", "复杂的情绪",
        "不知为何", "莫名地", "与此同时", "总而言之", "值得一提的是",
        "仿佛整个世界都", "一种说不清道不明的",
    ],
}


def _iter_terms() -> List[tuple]:
    """展开词库为 (term, category) 列表，按词长倒序，长词优先命中。"""
    pairs = []
    for category, terms in CLICHE_TERMS.items():
        for term in terms:
            pairs.append((term, category))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def scan_cliches(content: str) -> dict:
    """扫描套路表达，返回 {"hits", "total", "density", "by_category"}。

    hits 元素为 {term, category, start, end}；density 为每千字命中数，保留一位小数。
    重叠去重：按词长倒序扫描，同一区间被长词命中后短词不再重复计数。
    """
    text = content or ""
    hits: List[dict] = []
    occupied: List[tuple] = []  # 已命中的字符区间，用于重叠去重

    for term, category in _iter_terms():
        start = text.find(term)
        while start != -1:
            end = start + len(term)
            if not any(start < o_end and end > o_start for o_start, o_end in occupied):
                hits.append(
                    {"term": term, "category": category, "start": start, "end": end}
                )
                occupied.append((start, end))
            start = text.find(term, start + 1)

    hits.sort(key=lambda item: item["start"])
    by_category = {category: 0 for category in CLICHE_TERMS}
    for hit in hits:
        by_category[hit["category"]] += 1

    total = len(hits)
    density = round(total / max(len(text), 1) * 1000, 1)
    return {
        "hits": hits,
        "total": total,
        "density": density,
        "by_category": by_category,
    }

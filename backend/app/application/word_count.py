"""作者显式篇幅范围及不依赖模型的正文计数。"""


def count_words(content: str) -> int:
    """按非空白 Unicode 字符计数；标点、emoji计入，空格和换行不计。"""
    return sum(not char.isspace() for char in content)


def normalize_word_limits(value: dict | None) -> dict | None:
    """统一公开请求和内部调用的范围，拒绝布尔、浮点和隐式类型转换。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"min_words", "max_words"}:
        raise ValueError("篇幅范围参数不合法")
    limits = {key: value.get(key) for key in ("min_words", "max_words")}
    if all(bound is None for bound in limits.values()):
        raise ValueError("篇幅范围至少设置一个边界")
    for bound in limits.values():
        if bound is not None and (type(bound) is not int or not 1 <= bound <= 1_000_000):
            raise ValueError("篇幅边界必须是 1 到 1000000 的整数")
    minimum, maximum = limits["min_words"], limits["max_words"]
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("最少字数不能大于最多字数")
    return limits


def word_limit_target(limits: dict) -> str:
    """仅格式化作者明确设置的数据，不从章前指令中猜测字数。"""
    minimum, maximum = limits["min_words"], limits["max_words"]
    if minimum is not None and maximum is not None:
        return f"需在 {minimum}–{maximum} 字之间"
    if minimum is not None:
        return f"至少 {minimum} 字"
    return f"至多 {maximum} 字"


def check_word_limits(content: str, limits: dict | None, title: str = "", *, saved: bool = False) -> dict:
    """对完整正文应用闭区间边界，返回可持久化的质量数据。"""
    word_count = count_words(content)
    limits = normalize_word_limits(limits)
    if limits is None:
        return {"word_count": word_count, "min_words": None, "max_words": None,
                "within_limits": None, "warnings": []}
    minimum, maximum = limits["min_words"], limits["max_words"]
    within_limits = (minimum is None or word_count >= minimum) and (maximum is None or word_count <= maximum)
    warnings = []
    if not within_limits:
        reason = f"少于作者设置的最少 {minimum} 字" if minimum is not None and word_count < minimum else f"超过作者设置的最多 {maximum} 字"
        prefix = f"《{title}》" if title else ""
        save_note = "正文已保存，" if saved else ""
        warnings.append(f"{prefix}{save_note}字数检查：{word_count} 字，{reason}；请核对并调整篇幅。")
    return {
        "word_count": word_count, "min_words": minimum, "max_words": maximum,
        "within_limits": within_limits, "warnings": warnings,
    }

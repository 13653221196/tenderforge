"""金额数字 ↔ 中文大写。

clean-room 实现，供报价检查与模板回填使用：
- ``parse_amount``：从"￥1,768万元""1768000.50元"这类混合写法里解析出 Decimal
- ``to_cn_amount``：数字 → 中文大写（壹佰柒拾陆万捌仟元整）
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
UNITS = ("", "拾", "佰", "仟")
GROUPS = ("", "万", "亿", "万亿")
MAX_YUAN = 10**16 - 1  # 万亿组封顶

_NUM_RE = re.compile(r"^[\s￥¥人民币人民币]*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(万|万元|亿元|亿|元|圆|圆整|元整)?[\s整正]*$")


def parse_amount(value: str | float | int | Decimal) -> Decimal | None:
    """把混合写法解析成"元"为单位的 Decimal；失败返回 None。"""
    if isinstance(value, Decimal):
        return value if value >= 0 else None
    if isinstance(value, (int, float)):
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None
        return d if d >= 0 else None
    s = (value or "").strip()
    if not s:
        return None
    m = _NUM_RE.match(s)
    if not m:
        return None
    num, unit = m.group(1), m.group(2) or ""
    try:
        d = Decimal(num.replace(",", "").replace("，", ""))
    except InvalidOperation:
        return None
    if unit.startswith("万"):
        d *= Decimal(10000)
    elif unit.startswith("亿"):
        d *= Decimal(100000000)
    return d if d >= 0 else None


def _group_cn(g: int) -> str:
    """四位一组内部读法，如 1068 → 壹仟零陆拾捌。"""
    out = ""
    zero = False
    for i in (3, 2, 1, 0):
        digit = (g // 10**i) % 10
        if digit == 0:
            if out:
                zero = True
            continue
        if zero:
            out += "零"
            zero = False
        out += CN_DIGITS[digit] + UNITS[i]
    return out


def _int_cn(n: int) -> str:
    if n == 0:
        return "零"
    if n > MAX_YUAN:
        raise ValueError("金额超出可表达范围")
    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    out = ""
    need_zero = False
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if g == 0:
            need_zero = True
            continue
        if out and (g < 1000 or need_zero):
            out += "零"
        out += _group_cn(g) + GROUPS[idx]
        need_zero = False
    return out


def to_cn_amount(value: str | float | int | Decimal) -> str:
    """数字 → 中文大写金额，如 1768000 → 壹佰柒拾陆万捌仟元整。

    输入支持数字或混合写法字符串；无法解析抛 ValueError。
    """
    d = parse_amount(value)
    if d is None:
        raise ValueError(f"无法解析金额：{value!r}")
    d = d.quantize(Decimal("0.01"))
    if d > Decimal(MAX_YUAN):
        raise ValueError("金额超出可表达范围")
    neg = d < 0
    d = abs(d)
    yuan = int(d)
    cents = int((d - yuan) * 100)
    jiao, fen = divmod(cents, 10)

    parts = []
    parts.append(_int_cn(yuan))
    parts.append("元")
    if jiao == 0 and fen == 0:
        parts.append("整")
    else:
        if jiao == 0 and fen > 0 and yuan > 0:
            parts.append("零")
        elif jiao > 0:
            parts.append(CN_DIGITS[jiao] + "角")
        if fen > 0:
            parts.append(CN_DIGITS[fen] + "分")
    text = "".join(parts)
    return ("负" + text) if neg else text

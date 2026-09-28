"""保障协议共享规则：到期状态推导、必填检查、服务期限写法统一都收在这里。

列表筛选、标记到期、续签三个入口共用本模块，不允许各自再写一份日期分支：

- 到期状态只由「到期日期」与「服务期限」推导：优先取明确的到期日期，
  没有时再按统一口径解析服务期限（区间取截止日、单日期取当天、时长按起始日推算）。
- 待签订、已终止这类与到期无关的流程状态原样保留，不参与推导。
- 边界数据（缺日期、日期写法解析不出来）沿用老办法：推导不推翻已保存的状态。
- 服务期限读的时候兼容历史写法，写的时候统一成 ISO（YYYY-MM-DD / 区间 a~b / 时长 n个月）。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# 续签/登记时必须能确认到的两个业务字段；协议编号仍由登记入口单独要求。
REQUIRED_FIELDS = ("服务单位", "保障项目")

# 与到期推导无关、不能被日期推导覆盖的流程状态。
FLOW_STATUSES = ("待签订", "已终止")
STATUS_ACTIVE = "履行中"
STATUS_EXPIRED = "已到期"

# 自动到期标记：只有走登记/续签落库、明确按日期管理的协议才按当天日期推导；
# 历史协议（种子数据）没有该标记，沿用其已保存状态，老数据一条不动。
DERIVED_FLAG = "到期自动推导"

_DATE_PATTERNS = (
    re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)
_RANGE_SEPARATORS = ("--", "~", "～", "至", "到", "—")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(年|个月|月|周|天|日)\s*$")
_DURATION_DAYS = {"年": 365, "个月": 30, "月": 30, "周": 7, "天": 1, "日": 1}


def _text(value: Any) -> str:
    """入口字段统一成去空白的字符串；None/数值也按老办法转成文本再解析。"""
    if value is None:
        return ""
    return str(value).strip()


def parse_date(value: Any) -> date | None:
    """把历史上各种日期写法解析成 date；解析不了返回 None（边界数据沿用老办法）。"""
    text = _text(value)
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None
    return None


def parse_duration_days(value: Any) -> int | None:
    """解析「1年 / 12个月 / 90天」这类时长写法，折成天数；不认识就返回 None。"""
    match = _DURATION_RE.match(_text(value))
    if not match:
        return None
    amount = float(match[1])
    days = amount * _DURATION_DAYS[match[2]]
    return int(days)


def _split_range(text: str) -> tuple[str, str] | None:
    for separator in _RANGE_SEPARATORS:
        if separator in text:
            start, _, end = text.partition(separator)
            return start.strip(), end.strip()
    return None


def resolve_expiry_date(values: dict[str, Any], *, start: date | None = None) -> date | None:
    """到期状态推导唯一认的日期口径。

    优先取「到期日期」；没有时解析「服务期限」：
    区间取截止日、单日期取当天、时长（如 12个月）按起始日折算。
    任何一步解析不出来都返回 None，调用方按边界数据处理（沿用原状态）。
    """
    explicit = parse_date(values.get("到期日期"))
    if explicit is not None:
        return explicit

    period = _text(values.get("服务期限"))
    if not period:
        return None

    parts = _split_range(period)
    if parts is not None:
        return parse_date(parts[1])

    single = parse_date(period)
    if single is not None:
        return single

    days = parse_duration_days(period)
    if days is not None:
        anchor = start or parse_date(values.get("签订日期")) or parse_date(values.get("开始日期"))
        if anchor is None:
            return None
        return anchor + timedelta(days=days)

    return None


def canonical_period(value: Any) -> str:
    """写入服务期限时统一写法；无法识别的内容原样保留（边界数据不动）。"""
    text = _text(value)
    if not text:
        return text

    parts = _split_range(text)
    if parts is not None:
        start, end = parse_date(parts[0]), parse_date(parts[1])
        if start is not None and end is not None:
            return f"{start.isoformat()}~{end.isoformat()}"
        return text

    single = parse_date(text)
    if single is not None:
        return single.isoformat()

    duration = parse_duration_days(text)
    if duration is not None:
        match = _DURATION_RE.match(text)
        assert match is not None
        amount = match[1].rstrip("0").rstrip(".") if "." in match[1] else match[1]
        return f"{amount}{match[2]}"

    return text


def canonical_expiry(value: Any) -> str:
    """写入到期日期时统一成 ISO；无法识别时原样保留。"""
    parsed = parse_date(value)
    return parsed.isoformat() if parsed is not None else _text(value)


def missing_required(values: dict[str, Any], *, extra: tuple[str, ...] = ()) -> list[str]:
    """保障项目、服务单位的必填检查统一走这里；登记入口再额外要求协议编号。"""
    return [field for field in (*extra, *REQUIRED_FIELDS) if not _text(values.get(field))]


def derive_status(
    values: dict[str, Any],
    *,
    current: str | None = None,
    today: date | None = None,
) -> str | None:
    """由到期日期与服务期限推导到期状态。

    返回「履行中 / 已到期」；数据不足以推导时返回 None，调用方沿用原状态（老办法）。
    到期日当天仍算履行中，次日起才算到期；待签订、已终止不参与推导。
    """
    if current in FLOW_STATUSES:
        return current
    expiry = resolve_expiry_date(values)
    if expiry is None:
        return None
    today = today or date.today()
    return STATUS_EXPIRED if expiry < today else STATUS_ACTIVE

"""保障协议的共用规则：到期状态推导与保障项目、服务单位的必填检查。

列表筛选、标记到期、续签三个入口都只准走 evaluate_agreement 这一份实现，
避免同一份协议在不同入口被各自的分支推成不一样的到期状态。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

# 服务期限/到期日期里的日期统一按 YYYY-MM-DD 文本识别
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

STATUS_PENDING = "待签订"
STATUS_ACTIVE = "履行中"
STATUS_EXPIRED = "已到期"
STATUS_TERMINATED = "已终止"
STATUS_ORDER = [STATUS_PENDING, STATUS_ACTIVE, STATUS_EXPIRED, STATUS_TERMINATED]

# 登记时必须给全的字段
REQUIRED_FIELDS = ["协议编号", "服务单位", "保障项目"]
# 三个入口共用的必填检查口径：保障项目、服务单位缺一不可
ENTRY_REQUIRED_FIELDS = ["服务单位", "保障项目"]


@dataclass(frozen=True)
class AgreementState:
    """一次评估的结果：到期状态 + 缺失的必填字段。"""

    status: str
    missing_fields: list[str]


def _effective_expire_date(entry: dict[str, Any]) -> str | None:
    """取协议的到期日：优先看到期日期，没填再从服务期限文本里取。

    服务期限可能是单个日期或一段区间文本，写法不统一，统一识别其中最后
    一个日期作为到期日；识别不出日期时返回 None（按未到期处理，沿用
    老办法）。
    """
    explicit = str(entry.get("到期日期") or "").strip()
    if explicit:
        return explicit
    period = str(entry.get("服务期限") or "").strip()
    dates = _DATE_RE.findall(period)
    return dates[-1] if dates else None


def missing_required_fields(entry: dict[str, Any]) -> list[str]:
    """保障项目、服务单位的必填检查，三个入口共用。"""
    return [
        field
        for field in ENTRY_REQUIRED_FIELDS
        if not str(entry.get(field) or "").strip()
    ]


def derive_expire_status(entry: dict[str, Any], *, today: date | None = None) -> str:
    """由到期日期与服务期限推出协议的到期状态。

    只有履行中的协议才会被推成已到期；待签订、已到期、已终止一律保持
    原状态，保证已有协议的状态不被推导改写。到期日当天仍算履行中
    （到期日 < 今天才算到期）；到期日期缺失或无法识别时，退而从服务
    期限里取，仍取不到就按未到期处理（边界数据沿用老办法）。
    """
    current = str(entry.get("status") or "")
    if current != STATUS_ACTIVE:
        return current
    expire_date = _effective_expire_date(entry)
    if expire_date is None:
        return STATUS_ACTIVE
    today_text = (today or date.today()).isoformat()
    if expire_date < today_text:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def evaluate_agreement(
    entry: dict[str, Any],
    *,
    today: date | None = None,
) -> AgreementState:
    """到期推导与必填检查的唯一入口：列表、标记到期、续签都调这里。"""
    return AgreementState(
        status=derive_expire_status(entry, today=today),
        missing_fields=missing_required_fields(entry),
    )

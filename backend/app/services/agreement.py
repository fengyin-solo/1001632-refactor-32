"""保障协议业务规则：状态流转、字段校验与筛选口径都收在这里。

到期推导与保障项目、服务单位的必填检查统一放在 ``agreement_rules``，
列表筛选、标记到期、续签三个入口共用同一份实现，本层不再各写日期分支。
"""
from __future__ import annotations

from typing import Any

from app.services.agreement_rules import (
    DERIVED_FLAG,
    FLOW_STATUSES,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    canonical_expiry,
    canonical_period,
    derive_status,
    missing_required,
)
from app.store import store

MODULE = "agreement"
# 登记时除保障项目、服务单位外还要有协议编号；后两项的检查由共享规则负责。
CREATE_REQUIRED_EXTRA = ("协议编号",)
STATUS_ORDER = ["待签订", "履行中", "已到期", "已终止"]
# 确认签订、终止协议属于流程流转；标记到期/续签的到期状态一律由日期推导产出。
FLOW_ACTIONS = {"确认签订": "履行中", "终止协议": "已终止"}


class AgreementService:
    def _effective_status(self, row: dict[str, Any]) -> str:
        """三个入口判断到期状态的唯一口径。

        历史协议（种子数据，没有到期自动推导标记）沿用已保存状态，数值与状态一律不动；
        走登记/续签落库的协议才由到期日期与服务期限推导，推导不出时也回落到已保存状态。
        """
        if not row.get(DERIVED_FLAG):
            return str(row.get("status") or "")
        derived = derive_status(row, current=row.get("status"))
        return derived if derived is not None else str(row.get("status") or "")

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("协议编号", ""))]
        if status:
            rows = [row for row in rows if self._effective_status(row) == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = missing_required(values, extra=CREATE_REQUIRED_EXTRA)
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry["协议编号"] = values.get("协议编号")
        entry["服务单位"] = values.get("服务单位")
        entry["保障项目"] = values.get("保障项目")
        if values.get("协议金额") is not None:
            entry["协议金额"] = values.get("协议金额")
        if str(values.get("服务期限") or "").strip():
            entry["服务期限"] = canonical_period(values["服务期限"])
        if str(values.get("签订人员") or "").strip():
            entry["签订人员"] = values.get("签订人员")
        if str(values.get("到期日期") or "").strip():
            entry["到期日期"] = canonical_expiry(values["到期日期"])
        entry["status"] = STATUS_ORDER[0]
        entry[DERIVED_FLAG] = True
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"保障协议 {entry_id} 不存在或已归档"

        if action == "续签":
            return self._renew(entry, values or {})

        if action == "标记到期":
            return self._mark_expired(entry)

        if action not in FLOW_ACTIONS:
            return None, f"动作「{action}」不属于保障协议可执行范围"

        target = FLOW_ACTIONS[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = False
        return entry, f"保障协议已{action}"

    def _mark_expired(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """标记到期：到期状态走共享推导，不另写日期分支。

        - 待签订、已终止这类流程状态不参与到期判断，动作拦下并说明；
        - 日期上确已到期则落「已到期」，仍在服务期限内则保持「履行中」；
        - 日期缺失/解析不出来的边界数据沿用老办法（按标记动作落「已到期」）。
        """
        if entry.get("status") in FLOW_STATUSES:
            return None, f"当前状态「{entry.get('status')}」不支持标记到期"
        derived = derive_status(entry, current=entry.get("status"))
        entry["status"] = STATUS_EXPIRED if derived is None else derived
        entry[DERIVED_FLAG] = True
        entry["pending"] = entry["status"] != STATUS_ORDER[-1]
        entry["abnormal"] = False
        if entry["status"] == STATUS_EXPIRED:
            return entry, "保障协议已标记到期"
        return entry, "协议仍在服务期限内，未标记到期"

    def _renew(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """续签：必填检查与到期推导都走共享规则，新期限写法统一后落库。

        到期日期与服务期限是同一期限的两种写法，续签时作为一组处理：
        提交了新期限就替掉旧期限（只传其一时清掉另一个旧值，避免老日期继续优先）；
        都没提交时保留原期限。待签订协议先走签订流程，已终止协议不允许续签。
        """
        if entry.get("status") in FLOW_STATUSES:
            return None, f"当前状态「{entry.get('status')}」不支持续签"
        merged = {**entry, **values}
        missing = missing_required(merged)
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"

        new_expiry = str(values.get("到期日期") or "").strip()
        new_period = str(values.get("服务期限") or "").strip()
        if new_expiry:
            entry["到期日期"] = canonical_expiry(values["到期日期"])
            if new_period:
                entry["服务期限"] = canonical_period(values["服务期限"])
            else:
                entry.pop("服务期限", None)
        elif new_period:
            entry["服务期限"] = canonical_period(values["服务期限"])
            entry.pop("到期日期", None)

        if str(values.get("服务单位") or "").strip():
            entry["服务单位"] = values["服务单位"]
        if str(values.get("保障项目") or "").strip():
            entry["保障项目"] = values["保障项目"]

        entry[DERIVED_FLAG] = True
        derived = derive_status(entry, current=STATUS_ACTIVE)
        entry["status"] = STATUS_ACTIVE if derived is None else derived
        entry["pending"] = entry["status"] != STATUS_ORDER[-1]
        entry["abnormal"] = False
        return entry, "保障协议已续签"

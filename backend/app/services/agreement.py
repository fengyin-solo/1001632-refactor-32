"""保障协议业务规则：状态流转、字段校验与筛选口径都收在这里。

到期状态推导与保障项目、服务单位的必填检查只有一份实现，见
agreement_rules.evaluate_agreement；列表筛选、标记到期、续签都调同一份，
不再各写分支。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store
from app.services.agreement_rules import (
    REQUIRED_FIELDS,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_ORDER,
    STATUS_TERMINATED,
    evaluate_agreement,
)

MODULE = "agreement"
ACTION_RULES = {"确认签订": "履行中", "标记到期": "已到期", "终止协议": "已终止", "续签": "履行中"}
NEGATIVE_ACTIONS = []
# 续签时允许顺带更新的期限字段
PERIOD_FIELDS = ("到期日期", "服务期限")


class AgreementService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        today: date | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("协议编号", ""))]
        if status:
            # 列表筛选的到期判断也走同一份推导：只在返回视图上体现，
            # 不改库里已有的状态与数值。
            rows = [
                row
                for row in rows
                if evaluate_agreement(row, today=today).status == status
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        today: date | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"保障协议 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于保障协议可执行范围"

        values = values or {}
        patch = {
            field: values[field]
            for field in PERIOD_FIELDS
            if str(values.get(field) or "").strip()
        }
        # 续签只更新服务期限时，旧的到期日期会和新期限冲突，推导会继续
        # 采用旧日期，故同步作废旧到期日期，让期限口径只剩服务期限一份。
        if action == "续签" and "服务期限" in patch and "到期日期" not in patch:
            patch["到期日期"] = ""
        # 续签成立后协议必须能被同一份推导认成履行中，否则列表/标记到期
        # 仍会把它算成已到期，三个入口又对不上；用续签后的视图先做预演。
        if action == "续签" and patch:
            preview = evaluate_agreement({**entry, **patch, "status": STATUS_ACTIVE}, today=today)
        else:
            preview = None
        # 三个入口共用同一份评估：到期推导 + 保障项目、服务单位必填检查。
        state = evaluate_agreement({**entry, **patch}, today=today)
        if state.missing_fields:
            return None, f"缺少必填字段：{'、'.join(state.missing_fields)}"

        if action == "标记到期":
            if state.status == STATUS_EXPIRED:
                target = STATUS_EXPIRED
            elif state.status != STATUS_ACTIVE:
                return None, f"协议当前为「{state.status}」，不能标记到期"
            else:
                return None, "协议尚未到达期日期，不能标记到期"
        elif action == "续签":
            if state.status != STATUS_EXPIRED:
                return None, "只有已到期的协议才能续签"
            if preview is None or preview.status != STATUS_ACTIVE:
                return None, "续签需提供未到期的到期日期或服务期限"
            if preview.missing_fields:
                return None, f"缺少必填字段：{'、'.join(preview.missing_fields)}"
            target = STATUS_ACTIVE
        else:
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

        entry.update(patch)
        entry["status"] = target
        entry["pending"] = target != STATUS_TERMINATED
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"保障协议已{action}"

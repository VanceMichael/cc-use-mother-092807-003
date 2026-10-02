"""岗位、权限矩阵与敏感资料访问控制。

角色：
- secretariat        秘书处：登记项目/地块、组织协商、暂停资金
- community_liaison  社区联络人：登记权利人与协商、处理申诉、查看敏感资料
- implementer        实施机构：建作业批次、上报灾害
- monitor            生态监测团队：建样方/协议、提交观测、确认监测结果
- finance            出资与核款人员：批准与执行付款（不得自行提交证据）
- auditor            审核人员：只读全量追溯

职责分离（在业务层强制）：
- 提交监测证据的人不能批准对应付款；
- finance 不能提交付款证据，monitor/implementer 不能批准或付款。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import PermissionDenied
from .storage import Database

ROLES = frozenset(
    {
        "secretariat",
        "community_liaison",
        "implementer",
        "monitor",
        "finance",
        "auditor",
    }
)

# 动作 → 允许的岗位
PERMISSIONS: dict[str, frozenset[str]] = {
    "project.register": frozenset({"secretariat"}),
    "parcel.register": frozenset({"secretariat"}),
    "baseline.record": frozenset({"secretariat", "monitor"}),
    "plan.record": frozenset({"secretariat", "implementer"}),
    "rights.record": frozenset({"secretariat", "community_liaison"}),
    "rights.view_sensitive": frozenset({"community_liaison", "auditor", "secretariat"}),
    "consultation.record": frozenset({"secretariat", "community_liaison"}),
    "grievance.handle": frozenset({"secretariat", "community_liaison"}),
    "batch.create": frozenset({"implementer", "secretariat"}),
    "batch.derive": frozenset({"implementer", "secretariat"}),
    "disaster.record": frozenset({"implementer", "monitor", "secretariat"}),
    "plot.create": frozenset({"monitor"}),
    "protocol.publish": frozenset({"monitor", "secretariat"}),
    "observation.submit": frozenset({"monitor", "implementer"}),
    "observation.review": frozenset({"monitor", "secretariat"}),
    "estimate.submit": frozenset({"monitor"}),
    "estimate.confirm": frozenset({"monitor", "secretariat"}),
    "funding.record": frozenset({"finance", "secretariat"}),
    "milestone.submit_evidence": frozenset({"implementer", "monitor", "secretariat"}),
    "milestone.approve": frozenset({"finance"}),
    "milestone.pay": frozenset({"finance"}),
    "milestone.suspend": frozenset({"secretariat", "finance"}),
    "benefit.record": frozenset({"community_liaison", "secretariat", "finance"}),
    "task.run": frozenset({"secretariat", "monitor", "finance", "community_liaison"}),
    "audit.read": frozenset({"auditor", "secretariat"}),
}

_READONLY_ROLES = {"auditor"}
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Actor:
    """当前操作人：具体人员 + 岗位。职责分离以 user_id 判定。"""

    user_id: str
    display_name: str
    role: str
    organization: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未知岗位: {self.role}")
        if not _USER_ID_RE.match(self.user_id):
            raise ValueError("用户标识不合法")


class AccessControl:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register_user(self, actor: Actor) -> Actor:
        self.db.execute(
            "INSERT INTO users(user_id, display_name, role, organization, created_at)"
            " VALUES (?,?,?,?,?)",
            (actor.user_id, actor.display_name, actor.role, actor.organization, now_iso()),
        )
        self.db.commit()
        return actor

    def require(self, actor: Actor, action: str, entity: tuple[str, str] = ("", ""), **detail: object) -> None:
        """权限校验，无论允许/拒绝都写审计日志；拒绝时抛 PermissionDenied。"""
        allowed_roles = PERMISSIONS.get(action)
        granted = allowed_roles is not None and actor.role in allowed_roles
        self.db.execute(
            "INSERT INTO audit_log(ts, actor_id, actor_role, action, entity_type,"
            " entity_id, result, detail) VALUES (?,?,?,?,?,?,?,?)",
            (
                now_iso(),
                actor.user_id,
                actor.role,
                action,
                entity[0],
                entity[1],
                "allowed" if granted else "denied",
                _detail_json(detail),
            ),
        )
        if not granted:
            raise PermissionDenied(f"岗位 {actor.role} 无权执行 {action}")

    def get_user(self, user_id: str) -> Actor:
        row = self.db.query_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        if row is None:
            raise PermissionDenied(f"未知用户: {user_id}")
        return Actor(
            user_id=row["user_id"],
            display_name=row["display_name"],
            role=row["role"],
            organization=row["organization"] or "",
        )

    def can_view_sensitive(self, actor: Actor) -> bool:
        return actor.role in PERMISSIONS["rights.view_sensitive"]


def _detail_json(detail: dict[str, object]) -> str:
    import json

    return json.dumps(detail, ensure_ascii=False, default=str)

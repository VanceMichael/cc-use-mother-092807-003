"""门面服务：装配存储、权限、地块、监测、作业、资金与持久任务。

默认任务处理器：

- due_monitoring：检查某地块某季节是否已有已采纳观测，未观测则持续到期提醒；
- grievance_consultation：跟踪申诉，达成 resolved/withdrawn 才结束协商；
- payment_suspension：阻塞（未决灾害/申诉）解除后自动解除里程碑暂停。
"""

from __future__ import annotations

from pathlib import Path

from .auth import AccessControl, Actor
from .funding import FundingService
from .monitoring import MonitoringService
from .parcels import ParcelRegistry
from .storage import Database
from .tasks import (
    DUE_MONITORING,
    GRIEVANCE_CONSULTATION,
    PAYMENT_SUSPENSION,
    TaskQueue,
)
from .work import WorkService


class RestorationService:
    """承诺与核证服务的单一入口。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db = Database(db_path)
        self.ac = AccessControl(self.db)
        self.parcels = ParcelRegistry(self.db, self.ac)
        self.monitoring = MonitoringService(self.db, self.ac)
        self.work = WorkService(self.db, self.ac)
        self.funding = FundingService(self.db, self.ac)
        self.tasks = TaskQueue(self.db, self.ac)
        self._register_default_task_handlers()
        # 重启恢复：任何进行中任务都复位为待处理
        self.recovered_tasks = self.tasks.recover()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "RestorationService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 任务入队便捷方法

    def schedule_due_monitoring(
        self, *, parcel_id: str, season: str, run_after: str | None = None
    ) -> str:
        return self.tasks.enqueue(
            DUE_MONITORING,
            ref_key=f"{parcel_id}:{season}",
            payload={"parcel_id": parcel_id, "season": season},
            run_after=run_after,
        )

    def schedule_grievance_consultation(self, grievance_id: str, run_after: str | None = None) -> str:
        return self.tasks.enqueue(
            GRIEVANCE_CONSULTATION,
            ref_key=grievance_id,
            payload={"grievance_id": grievance_id},
            run_after=run_after,
        )

    def schedule_payment_suspension(self, milestone_id: str, run_after: str | None = None) -> str:
        return self.tasks.enqueue(
            PAYMENT_SUSPENSION,
            ref_key=milestone_id,
            payload={"milestone_id": milestone_id},
            run_after=run_after,
        )

    # -------------------------------------------------------------- 默认处理器

    def _register_default_task_handlers(self) -> None:
        def due_monitoring(payload: dict) -> tuple[bool, str]:
            rows = self.db.query_all(
                "SELECT o.status FROM observations o"
                " JOIN plots p ON p.plot_id = o.plot_id"
                " WHERE p.parcel_id=? AND o.season=?",
                (payload["parcel_id"], payload["season"]),
            )
            if any(r["status"] == "accepted" for r in rows):
                return True, "已有采纳观测，到期监测完成"
            return False, f"{payload['season']} 季节尚无采纳观测，继续等待"

        def grievance_consultation(payload: dict) -> tuple[bool, str]:
            row = self.db.query_one(
                "SELECT status FROM grievances WHERE grievance_id=?",
                (payload["grievance_id"],),
            )
            if row is None:
                return True, "申诉不存在，结束任务"
            if row["status"] in ("resolved", "withdrawn"):
                return True, f"申诉已结案（{row['status']}）"
            return False, f"申诉仍为 {row['status']}，继续协商"

        def payment_suspension(payload: dict) -> tuple[bool, str]:
            milestone_id = payload["milestone_id"]
            milestone = self.db.query_one(
                "SELECT * FROM payment_milestones WHERE milestone_id=?",
                (milestone_id,),
            )
            if milestone is None:
                return True, "里程碑不存在，结束任务"
            if milestone["status"] in ("withheld", "paid"):
                return True, f"里程碑已{milestone['status']}，暂停任务结束"
            blockers = self.funding._has_open_blockers(milestone["parcel_id"])
            if blockers:
                return False, "未决灾害或申诉仍存在，继续暂停"
            if milestone["status"] == "suspended":
                target = "evidence_submitted" if milestone["submitted_by"] else "pending"
                self.db.execute(
                    "UPDATE payment_milestones SET status=?, hold_reason='' WHERE milestone_id=?",
                    (target, milestone_id),
                )
                self.db.commit()
                return True, f"阻塞解除，里程碑恢复为 {target}"
            return True, "里程碑未处于暂停状态，无需处理"

        self.tasks.register_handler(DUE_MONITORING, due_monitoring)
        self.tasks.register_handler(GRIEVANCE_CONSULTATION, grievance_consultation)
        self.tasks.register_handler(PAYMENT_SUSPENSION, payment_suspension)

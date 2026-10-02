"""资金来源、付款里程碑与社区收益核证。

付款生命周期与职责分离：

    pending --提交证据--> evidence_submitted --财务批准--> approved --执行付款--> paid

* 提交证据的人员（实施/监测/秘书处）与批准付款的财务人员必须是不同的人，
  且财务岗位本身无权提交证据——任何一方都无法独自把钱付出；
* 证据只能引用“已确认”的估算结果，并关联到具体作业批次；
* 发生灾害或未决申诉时里程碑可暂停（suspended），阻塞解除前不得批准或付款；
* 每笔里程碑都能追溯到地块、证据（观测→估算→批次→灾害）与社区权益
  （权利人、协商结论、社区收益）。
"""

from __future__ import annotations

import uuid
from typing import Any

from .auth import Actor, AccessControl, now_iso
from .errors import MilestoneStateError, SeparationOfDutiesError
from .storage import Database, dumps, loads

VALID_STAGES = (
    "mobilization",
    "establishment",
    "midterm_verification",
    "final_verification",
    "benefit_sharing",
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FundingService:
    def __init__(self, db: Database, ac: AccessControl) -> None:
        self.db = db
        self.ac = ac

    # --------------------------------------------------------------- 资金来源

    def record_funding_source(
        self, actor: Actor, *, funder_name: str, currency: str, total_committed_minor: int
    ) -> str:
        self.ac.require(actor, "funding.record", ("funding_source", funder_name))
        source_id = _new_id("src")
        self.db.execute(
            "INSERT INTO funding_sources(source_id, funder_name, currency,"
            " total_committed_minor) VALUES (?,?,?,?)",
            (source_id, funder_name, currency, total_committed_minor),
        )
        self.db.commit()
        return source_id

    def allocate(self, actor: Actor, *, source_id: str, parcel_id: str, amount_minor: int) -> None:
        self.ac.require(actor, "funding.record", ("funding_allocation", parcel_id))
        total = self.db.query_one(
            "SELECT COALESCE(SUM(amount_minor),0) AS used,"
            " (SELECT total_committed_minor FROM funding_sources WHERE source_id=?) AS cap"
            " FROM funding_allocations WHERE source_id=?",
            (source_id, source_id),
        )
        cap = total["cap"] or 0
        if (total["used"] or 0) + amount_minor > cap:
            raise MilestoneStateError("分配金额超过出资方承诺总额")
        self.db.execute(
            "INSERT INTO funding_allocations(source_id, parcel_id, amount_minor)"
            " VALUES (?,?,?) ON CONFLICT(source_id, parcel_id)"
            " DO UPDATE SET amount_minor=excluded.amount_minor",
            (source_id, parcel_id, amount_minor),
        )
        self.db.commit()

    # ---------------------------------------------------------------- 里程碑

    def create_milestone(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        source_id: str,
        stage: str,
        amount_minor: int,
    ) -> str:
        self.ac.require(actor, "funding.record", ("payment_milestone", parcel_id))
        if stage not in VALID_STAGES:
            raise ValueError("付款阶段非法")
        milestone_id = _new_id("mil")
        self.db.execute(
            "INSERT INTO payment_milestones(milestone_id, parcel_id, source_id, stage,"
            " amount_minor, status, created_at) VALUES (?,?,?,?,?, 'pending',?)",
            (milestone_id, parcel_id, source_id, stage, amount_minor, now_iso()),
        )
        self.db.commit()
        return milestone_id

    def submit_evidence(
        self,
        actor: Actor,
        *,
        milestone_id: str,
        estimate_ids: list[str],
        batch_id: str,
    ) -> None:
        self.ac.require(actor, "milestone.submit_evidence", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        if milestone["status"] != "pending":
            raise MilestoneStateError(f"里程碑处于 {milestone['status']}，不能提交证据")
        if not estimate_ids:
            raise ValueError("至少需要一条已确认估算作为证据")
        for estimate_id in estimate_ids:
            row = self.db.query_one(
                "SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,)
            )
            if row is None:
                raise KeyError(f"估算证据不存在: {estimate_id}")
            if row["parcel_id"] != milestone["parcel_id"]:
                raise ValueError("估算证据不属于该里程碑地块")
            if row["status"] != "confirmed":
                raise MilestoneStateError("只能引用已确认的估算结果作为付款证据")

        batch = self.db.query_one("SELECT * FROM work_batches WHERE batch_id=?", (batch_id,))
        if batch is None or batch["parcel_id"] != milestone["parcel_id"]:
            raise ValueError("作业批次不存在或不属于该地块")

        self.db.execute(
            "UPDATE payment_milestones SET status='evidence_submitted', evidence_estimate_ids=?,"
            " evidence_batch_id=?, submitted_by=?, submitted_at=? WHERE milestone_id=?",
            (dumps(estimate_ids), batch_id, actor.user_id, now_iso(), milestone_id),
        )
        self.db.commit()

    def approve(self, actor: Actor, milestone_id: str) -> None:
        self.ac.require(actor, "milestone.approve", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        # 核心职责分离：提交证据的人不能批准付款；财务岗位也无法自己提交证据
        if milestone["submitted_by"] == actor.user_id:
            raise SeparationOfDutiesError("提交监测证据的人员不能独自批准该笔付款")
        if milestone["status"] != "evidence_submitted":
            raise MilestoneStateError(f"里程碑状态 {milestone['status']}，不能批准")
        if self._has_open_blockers(milestone["parcel_id"]):
            raise MilestoneStateError("地块存在未决灾害或申诉，付款前必须先暂停核查")
        self.db.execute(
            "UPDATE payment_milestones SET status='approved', approved_by=?, approved_at=?"
            " WHERE milestone_id=?",
            (actor.user_id, now_iso(), milestone_id),
        )
        self.db.commit()

    def pay(self, actor: Actor, milestone_id: str) -> None:
        self.ac.require(actor, "milestone.pay", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        if milestone["submitted_by"] == actor.user_id:
            raise SeparationOfDutiesError("提交证据与执行付款不得为同一人")
        if milestone["status"] != "approved":
            raise MilestoneStateError("只有已批准里程碑可以付款")
        self.db.execute(
            "UPDATE payment_milestones SET status='paid', paid_by=?, paid_at=?"
            " WHERE milestone_id=?",
            (actor.user_id, now_iso(), milestone_id),
        )
        self.db.commit()

    def suspend(self, actor: Actor, milestone_id: str, reason: str) -> None:
        self.ac.require(actor, "milestone.suspend", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        if milestone["status"] in ("paid", "withheld"):
            raise MilestoneStateError("已付款或已扣留的里程碑不能暂停")
        self.db.execute(
            "UPDATE payment_milestones SET status='suspended', hold_reason=? WHERE milestone_id=?",
            (reason, milestone_id),
        )
        self.db.commit()

    def release_suspension(self, actor: Actor, milestone_id: str) -> None:
        """阻塞解除后，按暂停前所处阶段回到 pending 或 evidence_submitted。"""
        self.ac.require(actor, "milestone.suspend", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        if milestone["status"] != "suspended":
            raise MilestoneStateError("里程碑未处于暂停状态")
        target = "evidence_submitted" if milestone["submitted_by"] else "pending"
        self.db.execute(
            "UPDATE payment_milestones SET status=?, hold_reason='' WHERE milestone_id=?",
            (target, milestone_id),
        )
        self.db.commit()

    def withhold(self, actor: Actor, milestone_id: str, reason: str) -> None:
        self.ac.require(actor, "milestone.suspend", ("payment_milestone", milestone_id))
        milestone = self._get(milestone_id)
        if milestone["status"] not in ("suspended", "evidence_submitted", "approved", "pending"):
            raise MilestoneStateError("当前状态不能扣留付款")
        self.db.execute(
            "UPDATE payment_milestones SET status='withheld', hold_reason=? WHERE milestone_id=?",
            (reason, milestone_id),
        )
        self.db.commit()

    def _has_open_blockers(self, parcel_id: str) -> bool:
        pending_disaster = self.db.query_one(
            "SELECT 1 FROM disaster_events WHERE parcel_id=? AND response='pending' LIMIT 1",
            (parcel_id,),
        )
        open_grievance = self.db.query_one(
            "SELECT 1 FROM grievances WHERE parcel_id=? AND status IN ('open','negotiating')"
            " LIMIT 1",
            (parcel_id,),
        )
        return pending_disaster is not None or open_grievance is not None

    def _get(self, milestone_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM payment_milestones WHERE milestone_id=?", (milestone_id,)
        )
        if row is None:
            raise KeyError("付款里程碑不存在")
        return dict(row)

    def get_milestone(self, milestone_id: str) -> dict[str, Any]:
        data = self._get(milestone_id)
        data["evidence_estimate_ids"] = loads(data["evidence_estimate_ids"])
        return data

    # ---------------------------------------------------------------- 社区收益

    def record_benefit(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        benefit_type: str,
        description: str,
        amount_minor: int = 0,
        recipient_count: int = 0,
        recorded_on: str,
        milestone_id: str | None = None,
    ) -> str:
        self.ac.require(actor, "benefit.record", ("community_benefit", parcel_id))
        benefit_id = _new_id("ben")
        self.db.execute(
            "INSERT INTO community_benefits(benefit_id, parcel_id, milestone_id,"
            " benefit_type, description, amount_minor, recipient_count, recorded_on)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                benefit_id,
                parcel_id,
                milestone_id,
                benefit_type,
                description,
                amount_minor,
                recipient_count,
                recorded_on,
            ),
        )
        self.db.commit()
        return benefit_id

    def list_benefits(self, parcel_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query_all(
                "SELECT * FROM community_benefits WHERE parcel_id=? ORDER BY recorded_on",
                (parcel_id,),
            )
        ]

    # ------------------------------------------------------------ 资金追溯报告

    def trace_payment(self, actor: Actor, milestone_id: str) -> dict[str, Any]:
        """组装一笔恢复资金的完整证据链：土地—证据—投入—社区权益。"""
        self.ac.require(actor, "audit.read", ("payment_milestone", milestone_id))
        m = self._get(milestone_id)
        parcel_id = m["parcel_id"]

        parcel = self.db.query_one("SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,))
        project = self.db.query_one(
            "SELECT * FROM projects WHERE project_id=?", (parcel["project_id"],)
        )
        source = self.db.query_one("SELECT * FROM funding_sources WHERE source_id=?", (m["source_id"],))

        estimates = []
        for estimate_id in loads(m["evidence_estimate_ids"]):
            row = self.db.query_one("SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,))
            if row is not None:
                estimates.append(
                    {
                        "estimate_id": row["estimate_id"],
                        "metric": row["metric"],
                        "unit": row["unit"],
                        "season": row["season"],
                        "model_version": row["model_version"],
                        "value": row["value"],
                        "status": row["status"],
                        "basis_observation_ids": loads(row["basis_observation_ids"]),
                    }
                )

        batch_chain = self._batch_chain(m["evidence_batch_id"])
        rights = []
        for r in self.db.query_all(
            "SELECT holder_id, holder_name, rights_type, tenure_doc_ref, sensitive"
            " FROM rights_holders WHERE parcel_id=? ORDER BY created_at",
            (parcel_id,),
        ):
            item = dict(r)
            item["sensitive"] = bool(r["sensitive"])
            rights.append(item)
        consultations = [
            {"held_on": r["held_on"], "outcome": r["outcome"], "minutes_ref": r["minutes_ref"]}
            for r in self.db.query_all(
                "SELECT held_on, outcome, minutes_ref FROM consultations"
                " WHERE parcel_id=? ORDER BY held_on",
                (parcel_id,),
            )
        ]
        benefits = [
            {
                "benefit_type": r["benefit_type"],
                "description": r["description"],
                "amount_minor": r["amount_minor"],
                "recipient_count": r["recipient_count"],
                "recorded_on": r["recorded_on"],
            }
            for r in self.db.query_all(
                "SELECT * FROM community_benefits WHERE parcel_id=? ORDER BY recorded_on",
                (parcel_id,),
            )
        ]
        disasters = [
            {
                "kind": r["kind"],
                "occurred_on": r["occurred_on"],
                "severity": r["severity"],
                "response": r["response"],
                "response_batch_id": r["response_batch_id"],
            }
            for r in self.db.query_all(
                "SELECT kind, occurred_on, severity, response, response_batch_id"
                " FROM disaster_events WHERE parcel_id=? ORDER BY occurred_on",
                (parcel_id,),
            )
        ]

        return {
            "milestone_id": milestone_id,
            "stage": m["stage"],
            "amount_minor": m["amount_minor"],
            "status": m["status"],
            "hold_reason": m["hold_reason"],
            "submitted_by": m["submitted_by"],
            "approved_by": m["approved_by"],
            "paid_by": m["paid_by"],
            "timeline": {
                "submitted_at": m["submitted_at"],
                "approved_at": m["approved_at"],
                "paid_at": m["paid_at"],
            },
            "land": {
                "parcel_id": parcel_id,
                "parcel_name": parcel["parcel_name"],
                "external_ref": parcel["external_ref"],
                "project_id": parcel["project_id"],
                "project_title": project["title"],
                "jurisdiction": project["jurisdiction"],
                "land_use": parcel["land_use"],
                "status": parcel["status"],
                "area_m2": parcel["area_m2"],
            },
            "funding_source": {
                "source_id": source["source_id"],
                "funder_name": source["funder_name"],
                "currency": source["currency"],
            },
            "evidence": {
                "estimates": estimates,
                "batch_chain": batch_chain,
            },
            "community_rights": {
                "rights_holders": rights,
                "consultations": consultations,
                "benefits": benefits,
            },
            "disasters": disasters,
        }

    def _batch_chain(self, batch_id: str | None) -> list[dict[str, Any]]:
        """沿前驱批次回溯完整作业链，包含失败原因与历史投入。"""
        chain: list[dict[str, Any]] = []
        current = batch_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            row = self.db.query_one("SELECT * FROM work_batches WHERE batch_id=?", (current,))
            if row is None:
                break
            chain.append(
                {
                    "batch_id": row["batch_id"],
                    "sequence_no": row["sequence_no"],
                    "season": row["season"],
                    "status": row["status"],
                    "planted_area_m2": row["planted_area_m2"],
                    "inputs_cost_minor": row["inputs_cost_minor"],
                    "failure_reason": row["failure_reason"],
                    "predecessor_batch_id": row["predecessor_batch_id"],
                }
            )
            current = row["predecessor_batch_id"]
        return chain

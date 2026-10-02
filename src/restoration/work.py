"""作业批次与灾害（火灾、病虫害等）。

失败不抹账：批次一旦标记 failed，其投入与失败原因永久保留；补植或
重做通过新批次承接，新批次以前驱批次指向历史，地块转入 rework。
灾害事件可关联到响应批次，形成“事件—失败—补植”的完整链路。
"""

from __future__ import annotations

import uuid
from typing import Any

from .auth import Actor, AccessControl, now_iso
from .storage import Database, dumps, loads


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class WorkService:
    def __init__(self, db: Database, ac: AccessControl) -> None:
        self.db = db
        self.ac = ac

    def create_batch(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        season: str,
        planted_area_m2: float,
        species_mix: list[str],
        inputs_cost_minor: int = 0,
    ) -> str:
        self.ac.require(actor, "batch.create", ("work_batch", parcel_id))
        next_no = self._next_sequence(parcel_id)
        batch_id = _new_id("bat")
        self.db.execute(
            "INSERT INTO work_batches(batch_id, parcel_id, sequence_no, season,"
            " planted_area_m2, species_mix, inputs_cost_minor, status,"
            " predecessor_batch_id, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?, 'established', ?,?,?)",
            (
                batch_id,
                parcel_id,
                next_no,
                season,
                planted_area_m2,
                dumps(species_mix),
                inputs_cost_minor,
                None,
                actor.user_id,
                now_iso(),
            ),
        )
        # 首批作业使地块进入 active
        if next_no == 1:
            self.db.execute("UPDATE parcels SET status='active' WHERE parcel_id=?", (parcel_id,))
        self.db.commit()
        return batch_id

    def _next_sequence(self, parcel_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(sequence_no),0) AS n FROM work_batches WHERE parcel_id=?",
            (parcel_id,),
        )
        return int(row["n"]) + 1

    def mark_batch_verified(self, actor: Actor, batch_id: str) -> None:
        self.ac.require(actor, "batch.derive", ("work_batch", batch_id))
        self._transition(batch_id, "verified", allowed={"established", "rework"})

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM work_batches WHERE batch_id=?", (batch_id,))
        if row is None:
            raise KeyError("作业批次不存在")
        data = dict(row)
        data["species_mix"] = loads(data["species_mix"])
        return data

    def list_batches(self, parcel_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM work_batches WHERE parcel_id=? ORDER BY sequence_no",
            (parcel_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 灾害

    def record_disaster(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        kind: str,
        occurred_on: str,
        area_affected_m2: float,
        severity: str,
        cause_note: str = "",
        batch_id: str | None = None,
    ) -> str:
        self.ac.require(actor, "disaster.record", ("disaster_event", parcel_id))
        event_id = _new_id("evt")
        self.db.execute(
            "INSERT INTO disaster_events(event_id, parcel_id, batch_id, kind,"
            " occurred_on, area_affected_m2, severity, cause_note, response,"
            " recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
            (
                event_id,
                parcel_id,
                batch_id,
                kind,
                occurred_on,
                area_affected_m2,
                severity,
                cause_note,
                actor.user_id,
                now_iso(),
            ),
        )
        self.db.commit()
        return event_id

    def respond_to_disaster(
        self,
        actor: Actor,
        *,
        event_id: str,
        response: str,
        season: str | None = None,
        species_mix: list[str] | None = None,
        planted_area_m2: float | None = None,
        inputs_cost_minor: int = 0,
    ) -> str | None:
        """对受灾地块给出补植(replant)/重做(redo)/认定损失(accepted_loss)。

        replant/redo 会把受影响批次（若有）标记 failed 并新建承接批次，
        历史投入与失败原因原样保留；redo 额外把地块置为 rework。
        """
        self.ac.require(actor, "batch.derive", ("disaster_event", event_id))
        if response not in ("replant", "redo", "accepted_loss"):
            raise ValueError("灾害响应类型非法")

        event = self.db.query_one("SELECT * FROM disaster_events WHERE event_id=?", (event_id,))
        if event is None:
            raise KeyError("灾害事件不存在")
        if event["response"] != "pending":
            raise ValueError("灾害事件已有响应，不得重复处理")

        new_batch_id: str | None = None
        parcel_id = event["parcel_id"]

        if response in ("replant", "redo"):
            if season is None or not species_mix or planted_area_m2 is None:
                raise ValueError("补植/重做必须提供季节、树种与面积")
            predecessor = event["batch_id"]
            if predecessor is not None:
                pred = self.get_batch(predecessor)
                reason = f"灾害事件 {event_id}（{event['kind']}/{event['severity']}）"
                self._transition(
                    predecessor,
                    "failed",
                    allowed={"established", "verified", "rework"},
                    failure_reason=reason,
                )
            new_batch_id = self._create_followup_batch(
                actor,
                parcel_id=parcel_id,
                season=season,
                species_mix=species_mix,
                planted_area_m2=planted_area_m2,
                inputs_cost_minor=inputs_cost_minor,
                predecessor=predecessor,
                rework=(response == "redo"),
            )
            if response == "redo":
                self.db.execute("UPDATE parcels SET status='rework' WHERE parcel_id=?", (parcel_id,))

        self.db.execute(
            "UPDATE disaster_events SET response=?, response_batch_id=? WHERE event_id=?",
            (response, new_batch_id, event_id),
        )
        self.db.commit()
        return new_batch_id

    def _create_followup_batch(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        season: str,
        species_mix: list[str],
        planted_area_m2: float,
        inputs_cost_minor: int,
        predecessor: str | None,
        rework: bool,
    ) -> str:
        batch_id = _new_id("bat")
        next_no = self._next_sequence(parcel_id)
        self.db.execute(
            "INSERT INTO work_batches(batch_id, parcel_id, sequence_no, season,"
            " planted_area_m2, species_mix, inputs_cost_minor, status,"
            " predecessor_batch_id, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?, ?,?,?,?)",
            (
                batch_id,
                parcel_id,
                next_no,
                season,
                planted_area_m2,
                dumps(species_mix),
                inputs_cost_minor,
                "rework" if rework else "established",
                predecessor,
                actor.user_id,
                now_iso(),
            ),
        )
        return batch_id

    def _transition(
        self,
        batch_id: str,
        target: str,
        *,
        allowed: set[str],
        failure_reason: str = "",
    ) -> None:
        row = self.db.query_one("SELECT * FROM work_batches WHERE batch_id=?", (batch_id,))
        if row is None:
            raise KeyError("作业批次不存在")
        if row["status"] not in allowed:
            raise ValueError(f"批次状态 {row['status']} 不能转为 {target}")
        self.db.execute(
            "UPDATE work_batches SET status=?, failure_reason=? WHERE batch_id=?",
            (target, failure_reason or row["failure_reason"], batch_id),
        )

    def get_disaster(self, event_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM disaster_events WHERE event_id=?", (event_id,))
        if row is None:
            raise KeyError("灾害事件不存在")
        return dict(row)

    def list_disasters(self, parcel_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query_all(
                "SELECT * FROM disaster_events WHERE parcel_id=? ORDER BY occurred_on",
                (parcel_id,),
            )
        ]

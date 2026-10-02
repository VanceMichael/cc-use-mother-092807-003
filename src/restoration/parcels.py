"""项目与地块登记：边界、权利人、退化基线、恢复方案、社区协商与申诉。

申报时对全部仍然有效的地块（registered/active/rework）做空间重叠检查，
重叠即拒绝并给出冲突明细，从源头防止同一片土地在不同项目中被重复计算。
"""

from __future__ import annotations

import uuid
from typing import Any

from .auth import Actor, AccessControl, now_iso
from .errors import ConflictError, PermissionDenied
from .geometry import polygon_area_m2, polygons_overlap
from .storage import Database, dumps, loads

# 互斥用途：同一空间上不得并存的土地用途
_INCOMPATIBLE_USES = {
    frozenset(("protection_forest", "mining")),
    frozenset(("protection_forest", "industrial_agriculture")),
    frozenset(("watershed_reserve", "mining")),
    frozenset(("watershed_reserve", "industrial_agriculture")),
    frozenset(("communal_forest", "mining")),
    frozenset(("restoration_zone", "mining")),
}

VALID_LAND_USES = (
    "restoration_zone",
    "protection_forest",
    "communal_forest",
    "watershed_reserve",
    "production_forest",
    "agroforestry_land",
    "smallholder_agriculture",
    "industrial_agriculture",
    "mining",
    "mangrove_zone",
)

LIVE_STATUSES = ("registered", "active", "rework")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ParcelRegistry:
    def __init__(self, db: Database, ac: AccessControl) -> None:
        self.db = db
        self.ac = ac

    # ------------------------------------------------------------------ 项目

    def register_project(self, actor: Actor, title: str, jurisdiction: str) -> str:
        self.ac.require(actor, "project.register", ("project", title))
        project_id = _new_id("prj")
        self.db.execute(
            "INSERT INTO projects(project_id, title, jurisdiction, created_at)"
            " VALUES (?,?,?,?)",
            (project_id, title, jurisdiction, now_iso()),
        )
        self.db.commit()
        return project_id

    # ------------------------------------------------------------------ 地块

    def register_parcel(
        self,
        actor: Actor,
        *,
        project_id: str,
        external_ref: str,
        parcel_name: str,
        geometry: dict,
        land_use: str,
    ) -> str:
        self.ac.require(actor, "parcel.register", ("parcel", external_ref), project_id=project_id)
        if land_use not in VALID_LAND_USES:
            raise ValueError(f"未知土地用途: {land_use}")
        area_m2 = polygon_area_m2(geometry)
        if area_m2 <= 0:
            raise ValueError("地块边界面积无效")

        # 幂等：同一项目同一外部编号重复申报直接返回既有地块，不重复建账
        existing = self.db.query_one(
            "SELECT parcel_id FROM parcels WHERE project_id=? AND external_ref=?",
            (project_id, external_ref),
        )
        if existing is not None:
            return existing["parcel_id"]

        conflicts = self._find_spatial_conflicts(geometry, land_use, exclude_id=None)
        if conflicts:
            raise ConflictError("地块申报未通过空间重叠与用途冲突检查", conflicts=conflicts)

        parcel_id = _new_id("par")
        self.db.execute(
            "INSERT INTO parcels(parcel_id, project_id, external_ref, parcel_name,"
            " geometry, area_m2, land_use, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                parcel_id,
                project_id,
                external_ref,
                parcel_name,
                dumps(geometry),
                area_m2,
                land_use,
                "registered",
                now_iso(),
            ),
        )
        self.db.commit()
        return parcel_id

    def _find_spatial_conflicts(
        self, geometry: dict, land_use: str, *, exclude_id: str | None
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in LIVE_STATUSES)
        rows = self.db.query_all(
            f"SELECT p.*, pr.title AS project_title FROM parcels p"
            f" JOIN projects pr ON pr.project_id = p.project_id"
            f" WHERE p.status IN ({placeholders})",
            LIVE_STATUSES,
        )
        conflicts: list[dict] = []
        for row in rows:
            if exclude_id and row["parcel_id"] == exclude_id:
                continue
            other_geom = loads(row["geometry"])
            if not polygons_overlap(geometry, other_geom):
                continue
            same_use = row["land_use"] == land_use
            incompatible = frozenset((land_use, row["land_use"])) in _INCOMPATIBLE_USES
            if same_use:
                kind = "duplicate_claim"
                message = "与既有地块用途相同且空间重叠，疑似重复申报"
            elif incompatible:
                kind = "incompatible_use"
                message = f"用途 {land_use} 与既有用途 {row['land_use']} 互斥"
            else:
                kind = "spatial_overlap"
                message = "与既有地块空间重叠"
            conflicts.append(
                {
                    "kind": kind,
                    "message": message,
                    "parcel_id": row["parcel_id"],
                    "project_id": row["project_id"],
                    "project_title": row["project_title"],
                    "land_use": row["land_use"],
                }
            )
        return conflicts

    def get_parcel(self, parcel_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,))
        if row is None:
            raise KeyError(f"地块不存在: {parcel_id}")
        data = dict(row)
        data["geometry"] = loads(data["geometry"])
        return data

    def set_status(self, parcel_id: str, status: str) -> None:
        if status not in ("registered", "active", "rework", "withdrawn"):
            raise ValueError("地块状态非法")
        self.db.execute("UPDATE parcels SET status=? WHERE parcel_id=?", (status, parcel_id))
        self.db.commit()

    # ---------------------------------------------------------------- 权利人

    def record_rights_holder(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        holder_name: str,
        rights_type: str,
        tenure_doc_ref: str = "",
        sensitive: bool = False,
    ) -> str:
        self.ac.require(actor, "rights.record", ("rights_holder", parcel_id))
        holder_id = _new_id("rhd")
        self.db.execute(
            "INSERT INTO rights_holders(holder_id, parcel_id, holder_name, rights_type,"
            " tenure_doc_ref, sensitive, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                holder_id,
                parcel_id,
                holder_name,
                rights_type,
                tenure_doc_ref,
                1 if sensitive else 0,
                now_iso(),
            ),
        )
        self.db.commit()
        return holder_id

    def list_rights_holders(self, actor: Actor, parcel_id: str) -> list[dict[str, Any]]:
        """敏感资料只向授权岗位开放；未授权时返回脱敏占位行。"""
        rows = self.db.query_all(
            "SELECT * FROM rights_holders WHERE parcel_id=? ORDER BY created_at",
            (parcel_id,),
        )
        may_view = self.ac.can_view_sensitive(actor)
        if not may_view and any(row["sensitive"] for row in rows):
            # 含敏感权利人时整表拒绝并留痕，避免逐字段泄露
            self.ac.require(
                actor,
                "rights.view_sensitive",
                ("rights_holder", parcel_id),
                parcel_id=parcel_id,
            )
        result = []
        for row in rows:
            data = dict(row)
            data["sensitive"] = bool(data["sensitive"])
            result.append(data)
        return result

    # ------------------------------------------------------------------ 基线

    def record_baseline(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        measured_on: str,
        canopy_cover_pct: float,
        soil_index: float,
        degradation_class: str,
        evidence_ref: str,
    ) -> None:
        self.ac.require(actor, "baseline.record", ("baseline", parcel_id))
        if not 0 <= canopy_cover_pct <= 100:
            raise ValueError("郁闭度必须在 0-100 之间")
        self.db.execute(
            "INSERT INTO baselines(parcel_id, measured_on, canopy_cover_pct, soil_index,"
            " degradation_class, evidence_ref, recorded_by) VALUES (?,?,?,?,?,?,?)",
            (
                parcel_id,
                measured_on,
                canopy_cover_pct,
                soil_index,
                degradation_class,
                evidence_ref,
                actor.user_id,
            ),
        )
        self.db.commit()

    def get_baseline(self, parcel_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM baselines WHERE parcel_id=?", (parcel_id,))
        return dict(row) if row else None

    # ------------------------------------------------------------------ 方案

    def record_plan(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        approach: str,
        target_canopy_pct: float,
        species_mix: list[str],
        monitor_seasons: list[str],
        plan_window_years: int,
    ) -> None:
        self.ac.require(actor, "plan.record", ("plan", parcel_id))
        if not monitor_seasons:
            raise ValueError("恢复方案必须声明适用监测季节")
        self.db.execute(
            "INSERT INTO restoration_plans(parcel_id, approach, target_canopy_pct,"
            " species_mix, monitor_seasons, plan_window_years, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                parcel_id,
                approach,
                target_canopy_pct,
                dumps(species_mix),
                dumps(monitor_seasons),
                plan_window_years,
                now_iso(),
            ),
        )
        self.db.commit()

    def get_plan(self, parcel_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM restoration_plans WHERE parcel_id=?", (parcel_id,))
        if row is None:
            return None
        data = dict(row)
        data["species_mix"] = loads(data["species_mix"])
        data["monitor_seasons"] = loads(data["monitor_seasons"])
        return data

    # ------------------------------------------------------------ 协商与申诉

    def record_consultation(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        held_on: str,
        outcome: str,
        minutes_ref: str,
        sensitive_notes: str = "",
    ) -> str:
        self.ac.require(actor, "consultation.record", ("consultation", parcel_id))
        consultation_id = _new_id("con")
        self.db.execute(
            "INSERT INTO consultations(consultation_id, parcel_id, held_on, outcome,"
            " minutes_ref, sensitive_notes, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                consultation_id,
                parcel_id,
                held_on,
                outcome,
                minutes_ref,
                sensitive_notes,
                now_iso(),
            ),
        )
        self.db.commit()
        return consultation_id

    def list_consultations(self, actor: Actor, parcel_id: str) -> list[dict[str, Any]]:
        """协商纪要中的敏感备注仅授权岗位可见。"""
        rows = self.db.query_all(
            "SELECT * FROM consultations WHERE parcel_id=? ORDER BY held_on",
            (parcel_id,),
        )
        may_view = self.ac.can_view_sensitive(actor)
        result = []
        for row in rows:
            data = dict(row)
            if data["sensitive_notes"] and not may_view:
                data["sensitive_notes"] = "【已脱敏：无授权】"
            result.append(data)
        return result

    def open_grievance(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        raised_by_name: str,
        description: str,
        raised_on: str,
        due_on: str,
    ) -> str:
        self.ac.require(actor, "grievance.handle", ("grievance", parcel_id))
        grievance_id = _new_id("grv")
        self.db.execute(
            "INSERT INTO grievances(grievance_id, parcel_id, raised_by_name,"
            " description, status, raised_on, due_on) VALUES (?,?,?,?, 'open', ?,?)",
            (grievance_id, parcel_id, raised_by_name, description, raised_on, due_on),
        )
        self.db.commit()
        return grievance_id

    def update_grievance(
        self, actor: Actor, grievance_id: str, status: str, resolution: str = ""
    ) -> None:
        self.ac.require(actor, "grievance.handle", ("grievance", grievance_id))
        if status not in ("open", "negotiating", "resolved", "withdrawn"):
            raise ValueError("申诉状态非法")
        self.db.execute(
            "UPDATE grievances SET status=?, resolution=? WHERE grievance_id=?",
            (status, resolution, grievance_id),
        )
        self.db.commit()

    def get_grievance(self, grievance_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM grievances WHERE grievance_id=?", (grievance_id,))
        return dict(row) if row else None

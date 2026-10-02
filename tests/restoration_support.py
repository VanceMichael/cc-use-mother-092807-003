"""测试公共夹具：内存服务、各岗位用户与已登记地块。"""

from __future__ import annotations

import unittest

from src.restoration import Actor, RestorationService


def box(lon: float, lat: float, dlon: float = 0.001, dlat: float = 0.001) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon, lat],
                [lon + dlon, lat],
                [lon + dlon, lat + dlat],
                [lon, lat + dlat],
                [lon, lat],
            ]
        ],
    }


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = RestorationService(":memory:")
        self.secretary = self._user("u_sec", "秘书处甲", "secretariat")
        self.liaison = self._user("u_lia", "联络人乙", "community_liaison")
        self.implementer = self._user("u_imp", "实施丙", "implementer")
        self.monitor = self._user("u_mon", "监测丁", "monitor")
        self.finance = self._user("u_fin", "财务戊", "finance")
        self.auditor = self._user("u_aud", "审核己", "auditor")

    def tearDown(self) -> None:
        self.svc.close()

    def _user(self, uid: str, name: str, role: str) -> Actor:
        actor = Actor(uid, name, role)
        self.svc.ac.register_user(actor)
        return actor

    def register_ready_parcel(
        self,
        geom: dict | None = None,
        *,
        land_use: str = "restoration_zone",
        external_ref: str = "P-001",
        seasons: list[str] | None = None,
    ) -> str:
        """登记一个已具备权利人、基线、方案的地块，返回地块标识。"""
        svc = self.svc
        project_id = svc.parcels.register_project(self.secretary, "示范项目", "某省某县")
        parcel_id = svc.parcels.register_parcel(
            self.secretary,
            project_id=project_id,
            external_ref=external_ref,
            parcel_name="一号地块",
            geometry=geom or box(100.0, 0.0),
            land_use=land_use,
        )
        svc.parcels.record_rights_holder(
            self.liaison,
            parcel_id=parcel_id,
            holder_name="某村集体（示例假名）",
            rights_type="communal",
            tenure_doc_ref="TENURE-EXAMPLE-1",
            sensitive=True,
        )
        svc.parcels.record_baseline(
            self.monitor,
            parcel_id=parcel_id,
            measured_on="2026-01-10",
            canopy_cover_pct=20.0,
            soil_index=0.3,
            degradation_class="severe",
            evidence_ref="BL-1",
        )
        svc.parcels.record_plan(
            self.secretary,
            parcel_id=parcel_id,
            approach="natural_regeneration",
            target_canopy_pct=60.0,
            species_mix=["本地阔叶树种"],
            monitor_seasons=seasons or ["dry", "wet"],
            plan_window_years=5,
        )
        return parcel_id

    def canopy_protocol(self, *, version: int = 1, seasons=None) -> str:
        return self.svc.monitoring.publish_protocol(
            self.monitor,
            method_key="canopy-line",
            version=version,
            metric="canopy_cover",
            unit="pct",
            applicable_seasons=seasons or ["dry", "wet"],
            definition_ref=f"METHOD-v{version}",
            valid_min=0,
            valid_max=100,
            delta_warn=40,
        )

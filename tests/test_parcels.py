"""地块申报：空间重叠、重复申报、用途冲突、敏感资料访问控制。"""

from src.restoration.errors import ConflictError, PermissionDenied
from tests.restoration_support import ServiceCase, box


class ParcelRegistrationTest(ServiceCase):
    def test_same_geometry_in_other_project_is_rejected_as_duplicate(self) -> None:
        parcel_id = self.register_ready_parcel()
        other_project = self.svc.parcels.register_project(self.secretary, "B项目", "他县")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.parcels.register_parcel(
                self.secretary,
                project_id=other_project,
                external_ref="Q-1",
                parcel_name="重复地块",
                geometry=box(100.0, 0.0),
                land_use="restoration_zone",
            )
        self.assertEqual(ctx.exception.conflicts[0]["kind"], "duplicate_claim")
        self.assertEqual(ctx.exception.conflicts[0]["parcel_id"], parcel_id)

    def test_partial_overlap_is_detected(self) -> None:
        self.register_ready_parcel()
        other_project = self.svc.parcels.register_project(self.secretary, "B项目", "他县")
        with self.assertRaises(ConflictError):
            self.svc.parcels.register_parcel(
                self.secretary,
                project_id=other_project,
                external_ref="Q-2",
                parcel_name="半重叠地块",
                geometry=box(100.0007, 0.0007),
                land_use="communal_forest",
            )

    def test_disjoint_parcel_accepted(self) -> None:
        self.register_ready_parcel()
        other_project = self.svc.parcels.register_project(self.secretary, "B项目", "他县")
        parcel_id = self.svc.parcels.register_parcel(
            self.secretary,
            project_id=other_project,
            external_ref="Q-3",
            parcel_name="不相邻地块",
            geometry=box(101.0, 1.0),
            land_use="restoration_zone",
        )
        self.assertTrue(parcel_id)

    def test_incompatible_land_use_reported(self) -> None:
        self.register_ready_parcel(land_use="protection_forest")
        other_project = self.svc.parcels.register_project(self.secretary, "矿权项目", "他县")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.parcels.register_parcel(
                self.secretary,
                project_id=other_project,
                external_ref="M-1",
                parcel_name="矿权重叠",
                geometry=box(100.0002, 0.0002),
                land_use="mining",
            )
        self.assertEqual(ctx.exception.conflicts[0]["kind"], "incompatible_use")

    def test_duplicate_external_ref_is_idempotent(self) -> None:
        project_id = self.svc.parcels.register_project(self.secretary, "示范项目", "某省某县")
        first = self.svc.parcels.register_parcel(
            self.secretary,
            project_id=project_id,
            external_ref="P-X",
            parcel_name="地块",
            geometry=box(100, 0),
            land_use="restoration_zone",
        )
        second = self.svc.parcels.register_parcel(
            self.secretary,
            project_id=project_id,
            external_ref="P-X",
            parcel_name="地块",
            geometry=box(100, 0),
            land_use="restoration_zone",
        )
        self.assertEqual(first, second)

    def test_implementer_cannot_register_parcel(self) -> None:
        project_id = self.svc.parcels.register_project(self.secretary, "项目", "县")
        with self.assertRaises(PermissionDenied):
            self.svc.parcels.register_parcel(
                self.implementer,
                project_id=project_id,
                external_ref="P-Z",
                parcel_name="越权地块",
                geometry=box(100, 0),
                land_use="restoration_zone",
            )


class SensitiveDataTest(ServiceCase):
    def test_sensitive_rights_hidden_from_finance_visible_to_liaison(self) -> None:
        parcel_id = self.register_ready_parcel()
        liaison_view = self.svc.parcels.list_rights_holders(self.liaison, parcel_id)
        self.assertEqual(liaison_view[0]["holder_name"], "某村集体（示例假名）")

        with self.assertRaises(PermissionDenied):
            self.svc.parcels.list_rights_holders(self.finance, parcel_id)

    def test_auditor_may_review_sensitive_data(self) -> None:
        parcel_id = self.register_ready_parcel()
        view = self.svc.parcels.list_rights_holders(self.auditor, parcel_id)
        self.assertEqual(len(view), 1)

    def test_consultation_sensitive_notes_are_masked(self) -> None:
        parcel_id = self.register_ready_parcel()
        self.svc.parcels.record_consultation(
            self.liaison,
            parcel_id=parcel_id,
            held_on="2026-02-01",
            outcome="conditional",
            minutes_ref="MIN-1",
            sensitive_notes="仅供授权岗位的敏感协商细节",
        )
        finance_view = self.svc.parcels.list_consultations(self.finance, parcel_id)
        self.assertIn("脱敏", finance_view[0]["sensitive_notes"])
        liaison_view = self.svc.parcels.list_consultations(self.liaison, parcel_id)
        self.assertEqual(liaison_view[0]["sensitive_notes"], "仅供授权岗位的敏感协商细节")

    def test_denied_access_is_audited(self) -> None:
        parcel_id = self.register_ready_parcel()
        with self.assertRaises(PermissionDenied):
            self.svc.parcels.list_rights_holders(self.finance, parcel_id)
        denied = self.svc.db.query_all(
            "SELECT * FROM audit_log WHERE result='denied' AND action='rights.view_sensitive'"
        )
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor_id"], "u_fin")

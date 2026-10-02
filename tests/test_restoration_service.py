"""校验森林景观恢复承诺与社区收益核证服务的业务规则。"""

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

from src.restoration_service import (
    Actor,
    CommitmentStatus,
    ConsultationStatus,
    DegradationBaseline,
    EvidenceRequirement,
    FundingSource,
    MilestoneStatus,
    ObservationStatus,
    ParcelStatus,
    RestorationPlan,
    RestorationService,
    ReviewCase,
    RightsHolder,
    Role,
    polygon_area,
    polygons_overlap,
)

SECRETARY = Actor("sec-1", frozenset({Role.SECRETARIAT}))
LIAISON = Actor("liaison-1", frozenset({Role.COMMUNITY_LIAISON}))
IMPLEMENTER = Actor("impl-1", frozenset({Role.IMPLEMENTER}))
MONITOR = Actor("mon-1", frozenset({Role.MONITOR}))
MONITOR2 = Actor("mon-2", frozenset({Role.MONITOR}))
FUNDER1 = Actor("fund-1", frozenset({Role.FUNDING_OFFICER}))
FUNDER2 = Actor("fund-2", frozenset({Role.FUNDING_OFFICER}))
AUDITOR = Actor("audit-1", frozenset({Role.AUDITOR}))

SQUARE_A = [(0, 0), (10, 0), (10, 10), (0, 10)]
SQUARE_B = [(20, 0), (30, 0), (30, 10), (20, 10)]
SQUARE_C = [(5, 5), (15, 5), (15, 15), (5, 15)]
SQUARE_D = [(10, 0), (20, 0), (20, 10), (10, 10)]

HOLDER = RightsHolder("holder-1", "河畔社区", "集体使用权", "敏感：联系方式与家庭构成")
BASELINE = DegradationBaseline("重度退化", 12.5, date(2025, 9, 30), "遥感与样地清查")
PLAN = RestorationPlan("植苗造林", ("本地阔叶",), 60.0, date(2026, 1, 1), date(2030, 12, 31))
FUNDING = (FundingSource("funder-a", "绿色气候基金示例", 500000.0, "USD"),)


def build_service() -> RestorationService:
    service = RestorationService()
    service.register_parcel(SECRETARY, "parcel-a", SQUARE_A, [HOLDER], BASELINE)
    return service


def apply(service: RestorationService, commitment_id: str, parcels, use="assisted_reforestation"):
    return service.submit_application(SECRETARY, commitment_id, parcels, use, PLAN, FUNDING)


def build_milestone_service() -> RestorationService:
    """地块 + 生效承诺 + 样方 + 一条已确认观测 + 一个待付款里程碑。"""
    service = build_service()
    apply(service, "com-1", ["parcel-a"])
    service.register_plot(
        SECRETARY,
        "plot-1",
        "parcel-a",
        method="固定样方实测",
        season="雨季",
        frequency_days=30,
        registered_on=date(2026, 3, 1),
    )
    service.record_observation(
        MONITOR, "plot-1", "obs-1", "carbon_stock", 42.0, "固定样方实测", "雨季", date(2026, 3, 5)
    )
    service.confirm_observation(AUDITOR, "plot-1", "obs-1")
    service.add_milestone(
        SECRETARY,
        "ms-1",
        "com-1",
        "第一阶段",
        10000.0,
        "USD",
        (EvidenceRequirement("carbon_stock", 1),),
    )
    return service


class GeometryTest(unittest.TestCase):
    def test_area_uses_shoelace(self) -> None:
        self.assertEqual(polygon_area(SQUARE_A), 100.0)

    def test_overlap_cases(self) -> None:
        self.assertTrue(polygons_overlap(SQUARE_A, SQUARE_C))
        self.assertTrue(polygons_overlap(SQUARE_A, [(2, 2), (4, 2), (4, 4), (2, 4)]))
        self.assertTrue(polygons_overlap(SQUARE_A, list(SQUARE_A)))
        self.assertFalse(polygons_overlap(SQUARE_A, SQUARE_B))
        self.assertFalse(polygons_overlap(SQUARE_A, SQUARE_D))  # 仅共边不算重叠


class ApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.service.register_parcel(SECRETARY, "parcel-b", SQUARE_B, [HOLDER], BASELINE)

    def test_disjoint_application_is_accepted(self) -> None:
        commitment = apply(self.service, "com-1", ["parcel-a"])
        self.assertIs(commitment.status, CommitmentStatus.ACTIVE)
        self.assertEqual(commitment.rejection_reasons, ())

    def test_same_use_overlap_is_rejected_as_double_counting(self) -> None:
        apply(self.service, "com-1", ["parcel-a"])
        self.service.register_parcel(SECRETARY, "parcel-c", SQUARE_C, [HOLDER], BASELINE)
        commitment = apply(self.service, "com-2", ["parcel-c"])
        self.assertIs(commitment.status, CommitmentStatus.REJECTED)
        self.assertIn("重复申报", commitment.rejection_reasons[0])

    def test_conflicting_use_overlap_is_rejected(self) -> None:
        apply(self.service, "com-1", ["parcel-a"])
        self.service.register_parcel(SECRETARY, "parcel-c", SQUARE_C, [HOLDER], BASELINE)
        commitment = apply(self.service, "com-3", ["parcel-c"], use="commercial_plantation")
        self.assertIs(commitment.status, CommitmentStatus.REJECTED)
        self.assertIn("用途冲突", commitment.rejection_reasons[0])

    def test_shared_boundary_is_not_overlap(self) -> None:
        apply(self.service, "com-1", ["parcel-a"])
        self.service.register_parcel(SECRETARY, "parcel-d", SQUARE_D, [HOLDER], BASELINE)
        commitment = apply(self.service, "com-4", ["parcel-d"])
        self.assertIs(commitment.status, CommitmentStatus.ACTIVE)

    def test_rejected_application_does_not_block_parcels(self) -> None:
        apply(self.service, "com-1", ["parcel-a"])
        self.service.register_parcel(SECRETARY, "parcel-c", SQUARE_C, [HOLDER], BASELINE)
        apply(self.service, "com-2", ["parcel-c"])
        commitment = apply(self.service, "com-5", ["parcel-c"])
        self.assertIs(commitment.status, CommitmentStatus.REJECTED)
        self.assertTrue(any("com-1" in reason for reason in commitment.rejection_reasons))
        self.assertFalse(any("com-2" in reason for reason in commitment.rejection_reasons))

    def test_application_requires_secretariat_and_known_parcels(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.submit_application(MONITOR, "com-x", ["parcel-a"], "use", PLAN, FUNDING)
        with self.assertRaisesRegex(ValueError, "地块不存在"):
            apply(self.service, "com-y", ["parcel-unknown"])
        with self.assertRaisesRegex(ValueError, "资金来源"):
            self.service.submit_application(
                SECRETARY, "com-z", ["parcel-a"], "use", PLAN, ()
            )


class MonitoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        apply(self.service, "com-1", ["parcel-a"])
        self.service.register_plot(
            SECRETARY,
            "plot-1",
            "parcel-a",
            method="固定样方实测",
            season="雨季",
            frequency_days=30,
            registered_on=date(2026, 3, 1),
        )

    def observe(self, key="obs-1", value=42.0, method="固定样方实测", season="雨季", actor=MONITOR):
        return self.service.record_observation(
            actor, "plot-1", key, "carbon_stock", value, method, season, date(2026, 3, 5)
        )

    def test_protocol_is_frozen_for_method_and_season(self) -> None:
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.observe(method="遥感反演")
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.observe(season="旱季")
        plot = self.service.get_plot("plot-1")
        with self.assertRaises(FrozenInstanceError):
            plot.protocol.method = "遥感反演"

    def test_observation_requires_monitor_role(self) -> None:
        with self.assertRaises(PermissionError):
            self.observe(actor=SECRETARY)

    def test_duplicate_observation_is_idempotent(self) -> None:
        first = self.observe()
        second = self.observe()
        self.assertIs(first, second)
        self.assertEqual(len(self.service.open_reviews()), 0)

    def test_divergent_observation_enters_review_and_keeps_original(self) -> None:
        self.observe()
        case = self.observe(value=43.0, actor=MONITOR2)
        self.assertIsInstance(case, ReviewCase)
        self.assertEqual(self.service.get_observation("plot-1", "obs-1").value, 42.0)
        self.assertEqual(len(self.service.open_reviews()), 1)

    def test_review_can_adopt_divergent_before_confirmation(self) -> None:
        self.observe()
        case = self.observe(value=43.0, actor=MONITOR2)
        self.service.resolve_review(SECRETARY, case.case_id, "divergent")
        observation = self.service.get_observation("plot-1", "obs-1")
        self.assertEqual(observation.value, 43.0)
        self.assertEqual(observation.submitted_by, "mon-2")
        self.assertIs(observation.status, ObservationStatus.PENDING)

    def test_confirmed_result_cannot_be_rewritten(self) -> None:
        self.observe()
        self.service.confirm_observation(AUDITOR, "plot-1", "obs-1")
        case = self.observe(value=50.0, actor=MONITOR2)
        with self.assertRaisesRegex(ValueError, "已确认"):
            self.service.resolve_review(SECRETARY, case.case_id, "divergent")
        self.service.resolve_review(SECRETARY, case.case_id, "original")
        self.assertEqual(self.service.get_observation("plot-1", "obs-1").value, 42.0)

    def test_submitter_cannot_confirm_own_observation(self) -> None:
        self.observe()
        dual = Actor("mon-1", frozenset({Role.MONITOR, Role.SECRETARIAT}))
        with self.assertRaises(PermissionError):
            self.service.confirm_observation(dual, "plot-1", "obs-1")

    def test_estimate_cannot_rewrite_confirmed_result(self) -> None:
        self.observe()
        self.service.confirm_observation(AUDITOR, "plot-1", "obs-1")
        self.service.record_estimate(
            MONITOR, "plot-1", "carbon_stock", 99.0, "model-v2", date(2026, 4, 1)
        )
        confirmed = self.service.confirmed_observations("plot-1", "carbon_stock")
        self.assertEqual([item.value for item in confirmed], [42.0])
        estimates = self.service.estimates("plot-1")
        self.assertEqual(estimates[0].model_version, "model-v2")
        self.assertEqual(estimates[0].value, 99.0)


class PaymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_milestone_service()

    def test_monitor_cannot_approve_even_with_funding_role(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.approve_milestone(MONITOR, "ms-1")
        dual = Actor("mon-1", frozenset({Role.MONITOR, Role.FUNDING_OFFICER}))
        with self.assertRaisesRegex(PermissionError, "提交监测的人不能批准付款"):
            self.service.approve_milestone(dual, "ms-1")

    def test_single_approver_cannot_release(self) -> None:
        self.service.approve_milestone(FUNDER1, "ms-1")
        with self.assertRaisesRegex(ValueError, "至少两名"):
            self.service.release_milestone(FUNDER2, "ms-1", date(2026, 6, 1))

    def test_two_distinct_officers_release_payment(self) -> None:
        self.service.approve_milestone(FUNDER1, "ms-1")
        self.service.approve_milestone(FUNDER1, "ms-1")  # 重复批准幂等
        self.service.approve_milestone(FUNDER2, "ms-1")
        milestone = self.service.release_milestone(FUNDER1, "ms-1", date(2026, 6, 1))
        self.assertIs(milestone.status, MilestoneStatus.RELEASED)
        self.assertEqual(milestone.approvals, ("fund-1", "fund-2"))
        self.assertEqual(milestone.released_by, "fund-1")
        self.assertEqual(milestone.released_on, date(2026, 6, 1))
        with self.assertRaisesRegex(ValueError, "已完成付款"):
            self.service.release_milestone(FUNDER2, "ms-1", date(2026, 6, 2))

    def test_release_requires_confirmed_evidence(self) -> None:
        self.service.add_milestone(
            SECRETARY,
            "ms-2",
            "com-1",
            "第二阶段",
            20000.0,
            "USD",
            (EvidenceRequirement("carbon_stock", 5),),
        )
        self.service.approve_milestone(FUNDER1, "ms-2")
        self.service.approve_milestone(FUNDER2, "ms-2")
        with self.assertRaisesRegex(ValueError, "监测证据不足"):
            self.service.release_milestone(FUNDER1, "ms-2", date(2026, 6, 1))

    def test_paused_milestone_cannot_be_approved_or_released(self) -> None:
        self.service.report_disturbance(
            MONITOR, "evt-1", "parcel-a", "fire", date(2026, 5, 1), "严重"
        )
        with self.assertRaisesRegex(ValueError, "暂停"):
            self.service.approve_milestone(FUNDER1, "ms-1")
        with self.assertRaisesRegex(ValueError, "暂停"):
            self.service.release_milestone(FUNDER1, "ms-1", date(2026, 6, 1))


class DisturbanceRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_milestone_service()
        self.service.record_work_batch(
            IMPLEMENTER, "batch-1", "com-1", "parcel-a", "整地与植苗", 8000.0, "USD", date(2026, 2, 10)
        )
        self.service.report_disturbance(
            MONITOR, "evt-1", "parcel-a", "fire", date(2026, 5, 1), "严重"
        )

    def test_disturbance_pauses_funding(self) -> None:
        self.assertIs(self.service.get_parcel("parcel-a").status, ParcelStatus.DISTURBED)
        self.assertIs(self.service.get_milestone("ms-1").status, MilestoneStatus.PAUSED)

    def test_recovery_keeps_failure_reason_and_investment(self) -> None:
        action = self.service.plan_recovery(
            SECRETARY, "rec-1", "parcel-a", "evt-1", "replant", "火灾烧毁八成苗木", date(2026, 5, 10)
        )
        self.assertEqual(action.failure_reason, "火灾烧毁八成苗木")
        self.assertIs(self.service.get_parcel("parcel-a").status, ParcelStatus.RECOVERING)
        investment = self.service.parcel_investment("parcel-a")
        self.assertEqual(investment["total_by_currency"], {"USD": 8000.0})
        self.assertEqual(len(investment["batches"]), 1)

    def test_resume_requires_completed_recovery(self) -> None:
        self.service.plan_recovery(
            SECRETARY, "rec-1", "parcel-a", "evt-1", "replant", "火灾烧毁八成苗木", date(2026, 5, 10)
        )
        with self.assertRaisesRegex(ValueError, "补植或重做"):
            self.service.resume_milestone(SECRETARY, "ms-1")
        self.service.complete_recovery(IMPLEMENTER, "rec-1")
        self.assertIs(self.service.get_parcel("parcel-a").status, ParcelStatus.ACTIVE)
        self.service.resume_milestone(SECRETARY, "ms-1")
        self.assertIs(self.service.get_milestone("ms-1").status, MilestoneStatus.PENDING)
        # 历史灾害与失败原因保留
        self.assertEqual(self.service.get_disturbance("evt-1").kind, "fire")
        self.assertEqual(self.service.get_recovery("rec-1").failure_reason, "火灾烧毁八成苗木")

    def test_recovery_requires_disturbed_parcel_and_reason(self) -> None:
        self.service.register_parcel(SECRETARY, "parcel-b", SQUARE_B, [HOLDER], BASELINE)
        with self.assertRaisesRegex(ValueError, "受灾状态"):
            self.service.plan_recovery(
                SECRETARY, "rec-2", "parcel-b", "evt-1", "replant", "原因", date(2026, 5, 10)
            )
        with self.assertRaisesRegex(ValueError, "失败原因"):
            self.service.plan_recovery(
                SECRETARY, "rec-3", "parcel-a", "evt-1", "redo", "  ", date(2026, 5, 10)
            )


class SensitiveDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_milestone_service()
        self.service.open_consultation(
            LIAISON, "cons-1", "com-1", "河畔社区", "敏感纪要：涉及采集权边界", date(2026, 2, 1)
        )

    def test_rights_holders_restricted_to_authorized_roles(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.rights_holders(MONITOR, "parcel-a")
        with self.assertRaises(PermissionError):
            self.service.rights_holders(FUNDER1, "parcel-a")
        holders = self.service.rights_holders(LIAISON, "parcel-a")
        self.assertEqual(holders[0].sensitive_detail, "敏感：联系方式与家庭构成")
        public = self.service.public_rights("parcel-a")
        self.assertNotIn("sensitive_detail", public[0])
        self.assertEqual(public[0]["display_name"], "河畔社区")

    def test_consultation_summary_restricted(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.consultation_detail(MONITOR, "cons-1")
        detail = self.service.consultation_detail(AUDITOR, "cons-1")
        self.assertIn("采集权", detail.summary)
        self.assertIs(self.service.consultation_status("cons-1"), ConsultationStatus.OPEN)

    def test_explain_payment_redacts_sensitive_fields_by_role(self) -> None:
        self.service.approve_milestone(FUNDER1, "ms-1")
        self.service.approve_milestone(FUNDER2, "ms-1")
        self.service.release_milestone(FUNDER1, "ms-1", date(2026, 6, 1))
        officer_view = self.service.explain_payment(FUNDER2, "ms-1")
        self.assertNotIn("sensitive_detail", officer_view["parcels"][0]["rights_holders"][0])
        self.assertNotIn("summary", officer_view["consultations"][0])
        auditor_view = self.service.explain_payment(AUDITOR, "ms-1")
        self.assertIn("sensitive_detail", auditor_view["parcels"][0]["rights_holders"][0])
        self.assertIn("summary", auditor_view["consultations"][0])
        with self.assertRaises(PermissionError):
            self.service.explain_payment(MONITOR, "ms-1")


class RestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_milestone_service()
        self.service.open_consultation(
            LIAISON, "cons-1", "com-1", "河畔社区", "敏感纪要", date(2026, 2, 1)
        )
        self.service.appeal_consultation(LIAISON, "cons-1")
        self.service.report_disturbance(
            MONITOR, "evt-1", "parcel-a", "pest", date(2026, 5, 1), "中等"
        )

    def test_pending_tasks_survive_restart(self) -> None:
        today = date(2026, 6, 1)
        before = self.service.pending_tasks(today)
        self.assertEqual(before["due_monitoring"], ["plot-1"])
        self.assertEqual(before["pending_appeals"], ["cons-1"])
        self.assertEqual(before["paused_funding"], ["ms-1"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            self.service.snapshot(path)
            restored = RestorationService.restore(path)
            self.assertEqual(restored.pending_tasks(today), before)
            observation = restored.get_observation("plot-1", "obs-1")
            self.assertIs(observation.status, ObservationStatus.CONFIRMED)

    def test_snapshot_round_trip_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            self.service.snapshot(first)
            RestorationService.restore(first).snapshot(second)
            self.assertEqual(first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))

    def test_restore_rejects_wrong_domain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            self.service.snapshot(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["domain"] = "other-domain"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "领域标识不一致"):
                RestorationService.restore(path)


class AuditTest(unittest.TestCase):
    def test_explain_payment_traces_land_evidence_and_rights(self) -> None:
        service = build_milestone_service()
        service.record_work_batch(
            IMPLEMENTER, "batch-1", "com-1", "parcel-a", "整地与植苗", 8000.0, "USD", date(2026, 2, 10)
        )
        service.open_consultation(
            LIAISON, "cons-1", "com-1", "河畔社区", "敏感纪要", date(2026, 2, 1)
        )
        service.agree_consultation(LIAISON, "cons-1")
        service.approve_milestone(FUNDER1, "ms-1")
        service.approve_milestone(FUNDER2, "ms-1")
        service.release_milestone(FUNDER1, "ms-1", date(2026, 6, 1))
        explain = service.explain_payment(AUDITOR, "ms-1")
        self.assertEqual(explain["milestone_id"], "ms-1")
        self.assertEqual(explain["status"], "released")
        self.assertEqual(explain["amount"], 10000.0)
        self.assertEqual(explain["approvals"], ["fund-1", "fund-2"])
        self.assertEqual(explain["parcels"][0]["parcel_id"], "parcel-a")
        self.assertEqual(explain["parcels"][0]["area"], 100.0)
        self.assertEqual(
            explain["parcels"][0]["rights_holders"][0]["right_type"], "集体使用权"
        )
        self.assertEqual(explain["evidence"][0]["key"], "obs-1")
        self.assertEqual(explain["evidence"][0]["submitted_by"], "mon-1")
        self.assertEqual(explain["funding"][0]["label"], "绿色气候基金示例")
        self.assertEqual(explain["consultations"][0]["community"], "河畔社区")
        self.assertEqual(explain["consultations"][0]["status"], "agreed")
        self.assertEqual(explain["investment"]["total_by_currency"], {"USD": 8000.0})


if __name__ == "__main__":
    unittest.main()

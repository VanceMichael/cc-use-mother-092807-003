"""资金核证：职责分离、暂停/阻塞、证据链追溯、承诺上限。"""

from src.restoration.errors import (
    MilestoneStateError,
    PermissionDenied,
    SeparationOfDutiesError,
)
from tests.restoration_support import ServiceCase


class FundingFlowTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.parcel_id = self.register_ready_parcel()
        protocol_id = self.canopy_protocol()
        self.plot_id = self.svc.monitoring.create_plot(
            self.monitor, parcel_id=self.parcel_id, plot_code="A1",
            location={"type": "Point", "coordinates": [100.0005, 0.0005]},
            established_season="dry",
        )
        self.obs = self.svc.monitoring.submit_observation(
            self.monitor, plot_id=self.plot_id, protocol_id=protocol_id,
            season="dry", observed_on="2026-03-01", value=25.0, idempotency_key="obs-1",
        )
        self.estimate_id = self.svc.monitoring.submit_estimate(
            self.monitor, parcel_id=self.parcel_id, metric="carbon", unit="tCO2e",
            season="dry", model_version="v1", value=120.0,
            basis_observation_ids=[self.obs["observation_id"]],
        )
        self.svc.monitoring.confirm_estimate(self.secretary, self.estimate_id)
        self.batch_id = self.svc.work.create_batch(
            self.implementer, parcel_id=self.parcel_id, season="dry",
            planted_area_m2=10_000, species_mix=["本地阔叶"], inputs_cost_minor=500_000,
        )
        self.source_id = self.svc.funding.record_funding_source(
            self.finance, funder_name="某绿色基金（示例）", currency="USD",
            total_committed_minor=10_000_000,
        )
        self.svc.funding.allocate(
            self.finance, source_id=self.source_id, parcel_id=self.parcel_id,
            amount_minor=8_000_000,
        )
        self.milestone_id = self.svc.funding.create_milestone(
            self.finance, parcel_id=self.parcel_id, source_id=self.source_id,
            stage="midterm_verification", amount_minor=2_000_000,
        )

    def test_finance_cannot_submit_evidence(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.funding.submit_evidence(
                self.finance, milestone_id=self.milestone_id,
                estimate_ids=[self.estimate_id], batch_id=self.batch_id,
            )

    def test_submitter_cannot_approve_own_evidence(self) -> None:
        # 让秘书处提交证据，再尝试由同一人批准——即使岗位有权也被职责分离阻止
        self.svc.funding.submit_evidence(
            self.secretary, milestone_id=self.milestone_id,
            estimate_ids=[self.estimate_id], batch_id=self.batch_id,
        )
        # 秘书处根本没有批准权限
        with self.assertRaises(PermissionDenied):
            self.svc.funding.approve(self.secretary, self.milestone_id)

        # 实施人提交、财务批准：不同的人是允许的（无阻塞时）
        milestone2 = self.svc.funding.create_milestone(
            self.finance, parcel_id=self.parcel_id, source_id=self.source_id,
            stage="establishment", amount_minor=1_000_000,
        )
        self.svc.funding.submit_evidence(
            self.implementer, milestone_id=milestone2,
            estimate_ids=[self.estimate_id], batch_id=self.batch_id,
        )
        self.svc.funding.approve(self.finance, milestone2)
        self.assertEqual(
            self.svc.funding.get_milestone(milestone2)["status"], "approved"
        )

    def test_unconfirmed_estimate_is_not_valid_evidence(self) -> None:
        raw = self.svc.monitoring.submit_estimate(
            self.monitor, parcel_id=self.parcel_id, metric="livelihood", unit="hh",
            season="dry", model_version="v1", value=10.0,
            basis_observation_ids=[self.obs["observation_id"]],
        )
        with self.assertRaises(MilestoneStateError):
            self.svc.funding.submit_evidence(
                self.implementer, milestone_id=self.milestone_id,
                estimate_ids=[raw], batch_id=self.batch_id,
            )

    def test_open_grievance_blocks_approval_until_suspended_and_released(self) -> None:
        self.svc.funding.submit_evidence(
            self.implementer, milestone_id=self.milestone_id,
            estimate_ids=[self.estimate_id], batch_id=self.batch_id,
        )
        grievance_id = self.svc.parcels.open_grievance(
            self.liaison, parcel_id=self.parcel_id, raised_by_name="某村民（示例假名）",
            description="边界异议", raised_on="2026-03-05", due_on="2026-04-05",
        )
        with self.assertRaises(MilestoneStateError):
            self.svc.funding.approve(self.finance, self.milestone_id)

        self.svc.funding.suspend(self.secretary, self.milestone_id, "申诉核查中")
        self.svc.parcels.update_grievance(self.liaison, grievance_id, "resolved", "重新指界")
        self.svc.funding.release_suspension(self.secretary, self.milestone_id)
        self.svc.funding.approve(self.finance, self.milestone_id)
        self.svc.funding.pay(self.finance, self.milestone_id)
        milestone = self.svc.funding.get_milestone(self.milestone_id)
        self.assertEqual(milestone["status"], "paid")
        self.assertNotEqual(milestone["submitted_by"], milestone["approved_by"])

    def test_pending_disaster_blocks_approval(self) -> None:
        m = self.svc.funding.create_milestone(
            self.finance, parcel_id=self.parcel_id, source_id=self.source_id,
            stage="final_verification", amount_minor=500_000,
        )
        self.svc.funding.submit_evidence(
            self.implementer, milestone_id=m,
            estimate_ids=[self.estimate_id], batch_id=self.batch_id,
        )
        self.svc.work.record_disaster(
            self.implementer, parcel_id=self.parcel_id, kind="fire",
            occurred_on="2026-04-01", area_affected_m2=100, severity="minor",
        )
        with self.assertRaises(MilestoneStateError):
            self.svc.funding.approve(self.finance, m)

    def test_allocation_cannot_exceed_commitment(self) -> None:
        with self.assertRaises(MilestoneStateError):
            self.svc.funding.allocate(
                self.finance, source_id=self.source_id, parcel_id=self.parcel_id,
                amount_minor=99_000_000,
            )

    def test_trace_payment_links_land_evidence_and_community_rights(self) -> None:
        self.svc.parcels.record_consultation(
            self.liaison, parcel_id=self.parcel_id, held_on="2026-02-01",
            outcome="consent", minutes_ref="MIN-1",
        )
        self.svc.funding.record_benefit(
            self.liaison, parcel_id=self.parcel_id, milestone_id=self.milestone_id,
            benefit_type="jobs", description="社区巡护岗位 30 个（示例）",
            recipient_count=30, recorded_on="2026-03-10",
        )
        # 制造一次火灾重做，验证批次链含失败历史
        event_id = self.svc.work.record_disaster(
            self.implementer, parcel_id=self.parcel_id, kind="fire",
            occurred_on="2026-04-01", area_affected_m2=2_000, severity="moderate",
            batch_id=self.batch_id,
        )
        b2 = self.svc.work.respond_to_disaster(
            self.implementer, event_id=event_id, response="replant", season="wet",
            species_mix=["本地阔叶"], planted_area_m2=2_000, inputs_cost_minor=80_000,
        )
        self.svc.funding.submit_evidence(
            self.implementer, milestone_id=self.milestone_id,
            estimate_ids=[self.estimate_id], batch_id=b2,
        )
        self.svc.funding.approve(self.finance, self.milestone_id)

        trace = self.svc.funding.trace_payment(self.auditor, self.milestone_id)
        self.assertEqual(trace["land"]["parcel_id"], self.parcel_id)
        self.assertEqual(trace["funding_source"]["source_id"], self.source_id)
        self.assertEqual(trace["evidence"]["estimates"][0]["estimate_id"], self.estimate_id)
        chain_ids = [b["batch_id"] for b in trace["evidence"]["batch_chain"]]
        self.assertEqual(chain_ids, [b2, self.batch_id])
        self.assertTrue(trace["evidence"]["batch_chain"][1]["failure_reason"])
        self.assertEqual(len(trace["community_rights"]["rights_holders"]), 1)
        self.assertEqual(trace["community_rights"]["consultations"][0]["outcome"], "consent")
        self.assertEqual(trace["community_rights"]["benefits"][0]["recipient_count"], 30)
        self.assertEqual(trace["disasters"][0]["kind"], "fire")

    def test_auditor_is_read_only(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.funding.suspend(self.auditor, self.milestone_id, "越权")

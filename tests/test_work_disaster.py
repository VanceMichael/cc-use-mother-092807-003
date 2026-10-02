"""灾害与补植/重做：历史投入与失败原因必须保留。"""

from tests.restoration_support import ServiceCase


class WorkAndDisasterTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.parcel_id = self.register_ready_parcel()

    def _batch(self, season: str = "dry", cost: int = 500_000) -> str:
        return self.svc.work.create_batch(
            self.implementer,
            parcel_id=self.parcel_id,
            season=season,
            planted_area_m2=10_000,
            species_mix=["本地阔叶"],
            inputs_cost_minor=cost,
        )

    def test_first_batch_activates_parcel(self) -> None:
        self._batch()
        self.assertEqual(self.svc.parcels.get_parcel(self.parcel_id)["status"], "active")

    def test_fire_redo_keeps_history_and_links_new_batch(self) -> None:
        b1 = self._batch()
        event_id = self.svc.work.record_disaster(
            self.implementer,
            parcel_id=self.parcel_id,
            kind="fire",
            occurred_on="2026-04-01",
            area_affected_m2=8_000,
            severity="severe",
            batch_id=b1,
        )
        b2 = self.svc.work.respond_to_disaster(
            self.implementer,
            event_id=event_id,
            response="redo",
            season="wet",
            species_mix=["耐火本地种"],
            planted_area_m2=8_000,
            inputs_cost_minor=300_000,
        )

        failed = self.svc.work.get_batch(b1)
        self.assertEqual(failed["status"], "failed")
        self.assertIn(event_id, failed["failure_reason"])
        self.assertEqual(failed["inputs_cost_minor"], 500_000)

        followup = self.svc.work.get_batch(b2)
        self.assertEqual(followup["predecessor_batch_id"], b1)
        self.assertEqual(self.svc.parcels.get_parcel(self.parcel_id)["status"], "rework")

        # 事件只能响应一次
        with self.assertRaises(ValueError):
            self.svc.work.respond_to_disaster(
                self.implementer,
                event_id=event_id,
                response="accepted_loss",
            )

    def test_replant_does_not_force_rework_status(self) -> None:
        b1 = self._batch()
        event_id = self.svc.work.record_disaster(
            self.implementer,
            parcel_id=self.parcel_id,
            kind="pest",
            occurred_on="2026-05-01",
            area_affected_m2=1_000,
            severity="minor",
            batch_id=b1,
        )
        self.svc.work.respond_to_disaster(
            self.implementer,
            event_id=event_id,
            response="replant",
            season="wet",
            species_mix=["本地阔叶"],
            planted_area_m2=1_000,
        )
        self.assertEqual(self.svc.parcels.get_parcel(self.parcel_id)["status"], "active")

    def test_history_is_complete_across_multiple_failures(self) -> None:
        b1 = self._batch(cost=500_000)
        e1 = self.svc.work.record_disaster(
            self.implementer, parcel_id=self.parcel_id, kind="drought",
            occurred_on="2026-04-01", area_affected_m2=10_000, severity="severe", batch_id=b1,
        )
        b2 = self.svc.work.respond_to_disaster(
            self.implementer, event_id=e1, response="replant", season="wet",
            species_mix=["耐旱种"], planted_area_m2=10_000, inputs_cost_minor=200_000,
        )
        e2 = self.svc.work.record_disaster(
            self.implementer, parcel_id=self.parcel_id, kind="fire",
            occurred_on="2026-09-01", area_affected_m2=10_000, severity="severe", batch_id=b2,
        )
        b3 = self.svc.work.respond_to_disaster(
            self.implementer, event_id=e2, response="redo", season="dry",
            species_mix=["耐火耐旱种"], planted_area_m2=10_000, inputs_cost_minor=250_000,
        )
        batches = self.svc.work.list_batches(self.parcel_id)
        self.assertEqual([b["sequence_no"] for b in batches], [1, 2, 3])
        self.assertEqual(self.svc.work.get_batch(b3)["predecessor_batch_id"], b2)
        self.assertEqual(sum(1 for b in batches if b["status"] == "failed"), 2)
        self.assertEqual(sum(b["inputs_cost_minor"] for b in batches), 950_000)

    def test_monitor_cannot_create_batch(self) -> None:
        from src.restoration.errors import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.svc.work.create_batch(
                self.monitor, parcel_id=self.parcel_id, season="dry",
                planted_area_m2=1, species_mix=["x"],
            )

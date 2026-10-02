"""持久任务：幂等入队、到期处理、等待重试、重启恢复。"""

import tempfile
import unittest
from pathlib import Path

from src.restoration import Actor, RestorationService
from tests.restoration_support import box


class TaskQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = RestorationService(":memory:")
        self.secretary = self._user("u_sec", "秘书处甲", "secretariat")
        self.liaison = self._user("u_lia", "联络人乙", "community_liaison")
        self.monitor = self._user("u_mon", "监测丁", "monitor")
        self._ready_parcel()

    def tearDown(self) -> None:
        self.svc.close()

    def _user(self, uid, name, role):
        actor = Actor(uid, name, role)
        self.svc.ac.register_user(actor)
        return actor

    def _ready_parcel(self):
        project_id = self.svc.parcels.register_project(self.secretary, "项目", "县")
        self.parcel_id = self.svc.parcels.register_parcel(
            self.secretary, project_id=project_id, external_ref="P-1",
            parcel_name="地块", geometry=box(100, 0), land_use="restoration_zone",
        )
        self.svc.parcels.record_baseline(
            self.monitor, parcel_id=self.parcel_id, measured_on="2026-01-10",
            canopy_cover_pct=20.0, soil_index=0.3,
            degradation_class="severe", evidence_ref="BL-1",
        )
        self.svc.parcels.record_plan(
            self.secretary, parcel_id=self.parcel_id, approach="natural_regeneration",
            target_canopy_pct=60.0, species_mix=["本地种"],
            monitor_seasons=["dry", "wet"], plan_window_years=5,
        )
        self.protocol_id = self.svc.monitoring.publish_protocol(
            self.monitor, method_key="canopy-line", version=1, metric="canopy_cover",
            unit="pct", applicable_seasons=["dry", "wet"], definition_ref="M1",
            valid_min=0, valid_max=100, delta_warn=40,
        )
        self.plot_id = self.svc.monitoring.create_plot(
            self.monitor, parcel_id=self.parcel_id, plot_code="A1",
            location={"type": "Point", "coordinates": [100.0005, 0.0005]},
            established_season="dry",
        )

    def test_due_monitoring_waits_then_completes(self) -> None:
        self.svc.schedule_due_monitoring(
            parcel_id=self.parcel_id, season="wet",
            run_after="2026-06-01T00:00:00+00:00",
        )
        waiting = self.svc.tasks.run_due(self.monitor, now="2026-06-01T00:00:00+00:00")
        self.assertEqual(waiting[0]["outcome"], "waiting")
        # 任务仍在队列中，且下次运行时间被推后
        self.assertEqual(self.svc.tasks.pending_count(), 1)
        run_after = self.svc.tasks.list_tasks()[0]["run_after"]
        self.assertGreater(run_after, "2026-06-01T00:00:00")

        self.svc.monitoring.submit_observation(
            self.monitor, plot_id=self.plot_id, protocol_id=self.protocol_id,
            season="wet", observed_on="2026-08-01", value=30.0,
            idempotency_key="obs-wet",
        )
        # 等到重试时间后任务完成
        done = self.svc.tasks.run_due(self.monitor, now="2026-08-02T00:00:00+00:00")
        self.assertEqual(done[0]["outcome"], "done")
        self.assertEqual(self.svc.tasks.pending_count(), 0)

    def test_enqueue_is_idempotent(self) -> None:
        t1 = self.svc.schedule_due_monitoring(parcel_id=self.parcel_id, season="wet")
        t2 = self.svc.schedule_due_monitoring(parcel_id=self.parcel_id, season="wet")
        self.assertEqual(t1, t2)
        self.assertEqual(len(self.svc.tasks.list_tasks()), 1)

    def test_grievance_consultation_tracks_to_resolution(self) -> None:
        gid = self.svc.parcels.open_grievance(
            self.liaison, parcel_id=self.parcel_id, raised_by_name="某村民（示例）",
            description="补偿异议", raised_on="2026-03-01", due_on="2026-04-01",
        )
        self.svc.schedule_grievance_consultation(gid, run_after="2026-03-02T00:00:00+00:00")
        waiting = self.svc.tasks.run_due(self.liaison, now="2026-03-02T00:00:00+00:00")
        self.assertEqual(waiting[0]["outcome"], "waiting")
        self.svc.parcels.update_grievance(self.liaison, gid, "resolved", "达成补充协议")
        done = self.svc.tasks.run_due(self.liaison, now="2026-04-02T00:00:00+00:00")
        self.assertEqual(done[0]["outcome"], "done")

    def test_payment_suspension_auto_releases_when_blocker_cleared(self) -> None:
        source = self.svc.funding.record_funding_source(
            self._user("u_fin2", "财务庚", "finance"),
            funder_name="基金（示例）", currency="USD", total_committed_minor=10_000_000,
        )
        finance2 = self.svc.ac.get_user("u_fin2")
        implementer = self._user("u_imp", "实施丙", "implementer")

        obs = self.svc.monitoring.submit_observation(
            self.monitor, plot_id=self.plot_id, protocol_id=self.protocol_id,
            season="dry", observed_on="2026-03-01", value=25.0, idempotency_key="obs-1",
        )
        est = self.svc.monitoring.submit_estimate(
            self.monitor, parcel_id=self.parcel_id, metric="carbon", unit="t",
            season="dry", model_version="v1", value=10.0,
            basis_observation_ids=[obs["observation_id"]],
        )
        self.svc.monitoring.confirm_estimate(self.secretary, est)
        batch = self.svc.work.create_batch(
            implementer, parcel_id=self.parcel_id, season="dry",
            planted_area_m2=10_000, species_mix=["本地种"],
        )
        milestone = self.svc.funding.create_milestone(
            finance2, parcel_id=self.parcel_id, source_id=source,
            stage="establishment", amount_minor=1_000_000,
        )
        self.svc.funding.submit_evidence(
            implementer, milestone_id=milestone, estimate_ids=[est], batch_id=batch,
        )
        gid = self.svc.parcels.open_grievance(
            self.liaison, parcel_id=self.parcel_id, raised_by_name="村民（示例）",
            description="异议", raised_on="2026-03-02", due_on="2026-04-02",
        )
        self.svc.funding.suspend(self.secretary, milestone, "申诉处理中")
        self.svc.schedule_payment_suspension(milestone, run_after="2026-03-03T00:00:00+00:00")

        waiting = self.svc.tasks.run_due(self.secretary, now="2026-03-03T00:00:00+00:00")
        self.assertEqual(waiting[0]["outcome"], "waiting")

        self.svc.parcels.update_grievance(self.liaison, gid, "resolved", "已协商")
        done = self.svc.tasks.run_due(self.secretary, now="2026-04-03T00:00:00+00:00")
        self.assertEqual(done[0]["outcome"], "done")
        self.assertEqual(
            self.svc.funding.get_milestone(milestone)["status"], "evidence_submitted"
        )


class RestartRecoveryTest(unittest.TestCase):
    def test_pending_and_in_progress_tasks_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "service.db"
            svc = RestorationService(db_path)
            sec = Actor("u_sec", "秘书处甲", "secretariat")
            svc.ac.register_user(sec)
            mon = Actor("u_mon", "监测丁", "monitor")
            svc.ac.register_user(mon)

            project_id = svc.parcels.register_project(sec, "项目", "县")
            parcel_id = svc.parcels.register_parcel(
                sec, project_id=project_id, external_ref="P-1", parcel_name="地块",
                geometry=box(100, 0), land_use="restoration_zone",
            )
            svc.parcels.record_baseline(
                mon, parcel_id=parcel_id, measured_on="2026-01-10",
                canopy_cover_pct=20.0, soil_index=0.3,
                degradation_class="severe", evidence_ref="BL-1",
            )
            svc.parcels.record_plan(
                sec, parcel_id=parcel_id, approach="natural_regeneration",
                target_canopy_pct=60.0, species_mix=["x"],
                monitor_seasons=["wet"], plan_window_years=5,
            )
            svc.schedule_due_monitoring(
                parcel_id=parcel_id, season="wet",
                run_after="2026-06-01T00:00:00+00:00",
            )
            # 手工把任务置为 in_progress，模拟进程在处理中崩溃
            svc.db.execute(
                "UPDATE task_queue SET status='in_progress' WHERE kind='due_monitoring'"
            )
            svc.db.commit()
            svc.close()

            # 重启：新服务实例复位进行中任务并继续等待观测
            svc2 = RestorationService(db_path)
            self.assertEqual(svc2.recovered_tasks, 1)
            sec2 = svc2.ac.get_user("u_sec")
            mon2 = svc2.ac.get_user("u_mon")
            results = svc2.tasks.run_due(sec2, now="2026-06-01T00:00:00+00:00")
            self.assertEqual(results[0]["outcome"], "waiting")

            protocol_id = svc2.monitoring.publish_protocol(
                mon2, method_key="c", version=1, metric="canopy_cover", unit="pct",
                applicable_seasons=["wet"], definition_ref="M1",
                valid_min=0, valid_max=100, delta_warn=40,
            )
            plot_id = svc2.monitoring.create_plot(
                mon2, parcel_id=parcel_id, plot_code="A1",
                location={"type": "Point", "coordinates": [100.0005, 0.0005]},
                established_season="wet",
            )
            svc2.monitoring.submit_observation(
                mon2, plot_id=plot_id, protocol_id=protocol_id, season="wet",
                observed_on="2026-08-01", value=30.0, idempotency_key="obs-wet",
            )
            # 等到重试窗口到期后任务完成（重试间隔仅 60 分钟）
            done = svc2.tasks.run_due(mon2, now="2026-08-02T00:00:00+00:00")
            self.assertEqual(done[0]["outcome"], "done")
            svc2.close()


if __name__ == "__main__":
    unittest.main()

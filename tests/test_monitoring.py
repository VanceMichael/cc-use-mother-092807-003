"""监测核证：方法/季节冻结、幂等、异文复核、确认结果不可改写。"""

from src.restoration.errors import (
    FrozenMethodError,
    ImmutableResultError,
    ReviewRequired,
)
from tests.restoration_support import ServiceCase


class MonitoringTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.parcel_id = self.register_ready_parcel()
        self.protocol_v1 = self.canopy_protocol(version=1)
        self.plot_id = self.svc.monitoring.create_plot(
            self.monitor,
            parcel_id=self.parcel_id,
            plot_code="A1",
            location={"type": "Point", "coordinates": [100.0005, 0.0005]},
            established_season="dry",
        )

    def _submit(self, key: str, value: float = 25.0, season: str = "dry", **kw):
        return self.svc.monitoring.submit_observation(
            self.monitor,
            plot_id=self.plot_id,
            protocol_id=kw.get("protocol_id", self.protocol_v1),
            season=season,
            observed_on="2026-03-01",
            value=value,
            idempotency_key=key,
        )

    def test_repeated_observation_is_idempotent(self) -> None:
        first = self._submit("obs-1")
        replay = self._submit("obs-1")
        self.assertEqual(first["observation_id"], replay["observation_id"])
        count = self.svc.db.query_one("SELECT COUNT(*) AS n FROM observations")["n"]
        self.assertEqual(count, 1)

    def test_out_of_range_value_enters_review(self) -> None:
        with self.assertRaises(ReviewRequired):
            self._submit("obs-high", value=150.0)
        row = self.svc.db.query_one(
            "SELECT * FROM observations WHERE idempotency_key='obs-high'"
        )
        self.assertEqual(row["status"], "pending_review")

    def test_delta_anomaly_enters_review_and_cannot_feed_estimate(self) -> None:
        with self.assertRaises(ReviewRequired):
            self._submit("obs-jump", value=90.0, season="wet")
        obs_id = self.svc.db.query_one(
            "SELECT observation_id FROM observations WHERE idempotency_key='obs-jump'"
        )["observation_id"]
        with self.assertRaises(ValueError):
            self.svc.monitoring.submit_estimate(
                self.monitor,
                parcel_id=self.parcel_id,
                metric="carbon",
                unit="tCO2e",
                season="wet",
                model_version="v1",
                value=1.0,
                basis_observation_ids=[obs_id],
            )

    def test_review_accept_makes_observation_usable(self) -> None:
        with self.assertRaises(ReviewRequired):
            self._submit("obs-jump2", value=88.0, season="wet")
        obs_id = self.svc.db.query_one(
            "SELECT observation_id FROM observations WHERE idempotency_key='obs-jump2'"
        )["observation_id"]
        # 采集人本人不能复核自己的异文数据
        with self.assertRaises(FrozenMethodError):
            self.svc.monitoring.review_observation(self.monitor, obs_id, accept=True)
        self.svc.monitoring.review_observation(
            self.secretary, obs_id, accept=True, note="确有先锋树种快速恢复"
        )
        reviewed = self.svc.monitoring.get_observation(obs_id)
        self.assertEqual(reviewed["status"], "accepted")

    def test_protocol_is_frozen_on_plot_after_first_observation(self) -> None:
        self._submit("obs-1")
        protocol_v2 = self.canopy_protocol(version=2)
        with self.assertRaises(FrozenMethodError):
            self._submit("obs-v2", value=26.0, protocol_id=protocol_v2)

    def test_season_outside_protocol_rejected(self) -> None:
        winter_protocol = self.svc.monitoring.publish_protocol(
            self.monitor,
            method_key="canopy-line",
            version=9,
            metric="canopy_cover",
            unit="pct",
            applicable_seasons=["winter"],
            definition_ref="M9",
        )
        # 新样方使用冬季协议：样方建立季节必须在方案季节内
        with self.assertRaises(FrozenMethodError):
            self.svc.monitoring.create_plot(
                self.monitor,
                parcel_id=self.parcel_id,
                plot_code="W1",
                location={"type": "Point", "coordinates": [100.0006, 0.0006]},
                established_season="winter",
            )
        plot2 = self.svc.monitoring.create_plot(
            self.monitor,
            parcel_id=self.parcel_id,
            plot_code="A2",
            location={"type": "Point", "coordinates": [100.0006, 0.0006]},
            established_season="wet",
        )
        with self.assertRaises(FrozenMethodError):
            self.svc.monitoring.submit_observation(
                self.monitor,
                plot_id=plot2,
                protocol_id=winter_protocol,
                season="winter",
                observed_on="2026-12-01",
                value=30.0,
                idempotency_key="obs-winter",
            )

    def test_confirmed_estimate_cannot_be_overwritten_by_new_model(self) -> None:
        obs = self._submit("obs-1")
        e1 = self.svc.monitoring.submit_estimate(
            self.monitor,
            parcel_id=self.parcel_id,
            metric="carbon",
            unit="tCO2e",
            season="dry",
            model_version="model-v1",
            value=120.0,
            basis_observation_ids=[obs["observation_id"]],
        )
        self.svc.monitoring.confirm_estimate(self.secretary, e1)

        # 后来的模型不能改写已确认结果
        with self.assertRaises(ImmutableResultError):
            self.svc.monitoring.submit_estimate(
                self.monitor,
                parcel_id=self.parcel_id,
                metric="carbon",
                unit="tCO2e",
                season="dry",
                model_version="model-v2",
                value=130.0,
                basis_observation_ids=[obs["observation_id"]],
            )

        # 重复确认无效
        with self.assertRaises(ImmutableResultError):
            self.svc.monitoring.confirm_estimate(self.secretary, e1)

    def test_new_season_may_use_new_model_without_touching_history(self) -> None:
        obs_dry = self._submit("obs-dry")
        e_dry = self.svc.monitoring.submit_estimate(
            self.monitor,
            parcel_id=self.parcel_id,
            metric="carbon",
            unit="tCO2e",
            season="dry",
            model_version="model-v1",
            value=120.0,
            basis_observation_ids=[obs_dry["observation_id"]],
        )
        self.svc.monitoring.confirm_estimate(self.secretary, e_dry)

        obs_wet = self._submit("obs-wet", value=28.0, season="wet")
        e_wet = self.svc.monitoring.submit_estimate(
            self.monitor,
            parcel_id=self.parcel_id,
            metric="carbon",
            unit="tCO2e",
            season="wet",
            model_version="model-v2",
            value=140.0,
            basis_observation_ids=[obs_wet["observation_id"]],
        )
        self.svc.monitoring.confirm_estimate(self.secretary, e_wet)
        self.assertEqual(self.svc.monitoring.get_estimate(e_dry)["value"], 120.0)

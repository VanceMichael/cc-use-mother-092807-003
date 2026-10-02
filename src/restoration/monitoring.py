"""监测核证：样方、采集方法协议、观测、估算结果。

三条不可破坏的时间纪律：

1. 采集方法冻结——样方的首批观测确定协议后，该样方时间序列只能继续
   使用同一协议；协议升版不得改动既有观测。
2. 适用季节冻结——观测只能落在协议与恢复方案共同声明的季节，样方
   建立后其季节集合不得改变。
3. 结果冻结——估算结果一经确认即不可变；之后出现的新模型版本只能
   另存草稿，不能改写或顶替同一（地块、指标、季节）已确认的结果。

观测以幂等键去重；越界或相对基线跳变异常的值进入复核，复核通过前
不参与任何估算。
"""

from __future__ import annotations

import uuid
from typing import Any

from .auth import Actor, AccessControl, now_iso
from .errors import FrozenMethodError, ImmutableResultError, ReviewRequired
from .storage import Database, dumps, loads

ACCEPTED = "accepted"
PENDING_REVIEW = "pending_review"
REJECTED = "rejected"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class MonitoringService:
    def __init__(self, db: Database, ac: AccessControl) -> None:
        self.db = db
        self.ac = ac

    # ------------------------------------------------------------- 方法协议

    def publish_protocol(
        self,
        actor: Actor,
        *,
        method_key: str,
        version: int,
        metric: str,
        unit: str,
        applicable_seasons: list[str],
        definition_ref: str,
        valid_min: float | None = None,
        valid_max: float | None = None,
        delta_warn: float | None = None,
    ) -> str:
        self.ac.require(actor, "protocol.publish", ("protocol", f"{method_key}@v{version}"))
        if not applicable_seasons:
            raise ValueError("协议必须声明适用季节")
        protocol_id = _new_id("prt")
        self.db.execute(
            "INSERT INTO protocols(protocol_id, method_key, version, metric, unit,"
            " valid_min, valid_max, delta_warn, applicable_seasons, definition_ref,"
            " locked, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                protocol_id,
                method_key,
                version,
                metric,
                unit,
                valid_min,
                valid_max,
                delta_warn,
                dumps(applicable_seasons),
                definition_ref,
                now_iso(),
            ),
        )
        self.db.commit()
        return protocol_id

    def get_protocol(self, protocol_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM protocols WHERE protocol_id=?", (protocol_id,))
        if row is None:
            raise KeyError(f"协议不存在: {protocol_id}")
        data = dict(row)
        data["applicable_seasons"] = loads(data["applicable_seasons"])
        data["locked"] = bool(data["locked"])
        return data

    # ------------------------------------------------------------------ 样方

    def create_plot(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        plot_code: str,
        location: dict,
        established_season: str,
    ) -> str:
        self.ac.require(actor, "plot.create", ("plot", f"{parcel_id}/{plot_code}"))
        plan = self._require_plan(parcel_id)
        if established_season not in plan["monitor_seasons"]:
            raise FrozenMethodError(
                f"建立季节 {established_season} 不在方案声明的适用季节内"
            )
        plot_id = _new_id("plt")
        self.db.execute(
            "INSERT INTO plots(plot_id, parcel_id, plot_code, location,"
            " protocol_id, established_season) VALUES (?,?,?,?,NULL,?)",
            (plot_id, parcel_id, plot_code, dumps(location), established_season),
        )
        self.db.commit()
        return plot_id

    def get_plot(self, plot_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM plots WHERE plot_id=?", (plot_id,))
        if row is None:
            raise KeyError(f"样方不存在: {plot_id}")
        data = dict(row)
        data["location"] = loads(data["location"])
        return data

    # ------------------------------------------------------------------ 观测

    def submit_observation(
        self,
        actor: Actor,
        *,
        plot_id: str,
        protocol_id: str,
        season: str,
        observed_on: str,
        value: float,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """提交一次观测。重复幂等键返回原观测；异常值进入复核。"""
        self.ac.require(actor, "observation.submit", ("observation", idempotency_key))

        # 幂等：同键重放直接返回首次结果，不产生第二条记录
        replay = self.db.query_one(
            "SELECT observation_id FROM observations WHERE idempotency_key=?",
            (idempotency_key,),
        )
        if replay is not None:
            return self.get_observation(replay["observation_id"])

        plot = self.get_plot(plot_id)
        parcel_id = plot["parcel_id"]
        protocol = self.get_protocol(protocol_id)
        plan = self._require_plan(parcel_id)

        # 方法冻结：样方协议一旦确定，后续观测不得换协议
        if plot["protocol_id"] is not None and plot["protocol_id"] != protocol_id:
            raise FrozenMethodError(
                "样方采集方法已冻结，不能改用新协议；请新建样方承载新方法"
            )
        # 季节冻结：协议与方案双重声明的季节才有效
        if season not in protocol["applicable_seasons"]:
            raise FrozenMethodError(
                f"季节 {season} 不属于协议 {protocol['method_key']} 的适用季节"
            )
        if season not in plan["monitor_seasons"]:
            raise FrozenMethodError(f"季节 {season} 不属于恢复方案的监测季节")

        status, note = self._screen_value(parcel_id, protocol, value)

        observation_id = _new_id("obs")
        self.db.execute(
            "INSERT INTO observations(observation_id, plot_id, protocol_id, season,"
            " observed_on, value, idempotency_key, collected_by, status, review_note,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id,
                plot_id,
                protocol_id,
                season,
                observed_on,
                value,
                idempotency_key,
                actor.user_id,
                status,
                note,
                now_iso(),
            ),
        )
        # 首批可追溯观测把协议冻结到样方上
        if plot["protocol_id"] is None:
            self.db.execute(
                "UPDATE plots SET protocol_id=? WHERE plot_id=?",
                (protocol_id, plot_id),
            )
            self.db.execute("UPDATE protocols SET locked=1 WHERE protocol_id=?", (protocol_id,))
        self.db.commit()

        result = self.get_observation(observation_id)
        if status == PENDING_REVIEW:
            raise ReviewRequired(note)
        return result

    def _screen_value(self, parcel_id: str, protocol: dict, value: float) -> tuple[str, str]:
        if protocol["valid_min"] is not None and value < protocol["valid_min"]:
            return PENDING_REVIEW, f"数值 {value} 低于协议下限 {protocol['valid_min']}"
        if protocol["valid_max"] is not None and value > protocol["valid_max"]:
            return PENDING_REVIEW, f"数值 {value} 高于协议上限 {protocol['valid_max']}"
        if protocol["delta_warn"] is not None:
            baseline = self.db.query_one(
                "SELECT canopy_cover_pct FROM baselines WHERE parcel_id=?", (parcel_id,)
            )
            if baseline is not None and abs(value - baseline["canopy_cover_pct"]) > protocol["delta_warn"]:
                return PENDING_REVIEW, "相对退化基线的变化超过异文阈值"
        return ACCEPTED, ""

    def review_observation(
        self, actor: Actor, observation_id: str, *, accept: bool, note: str = ""
    ) -> None:
        self.ac.require(actor, "observation.review", ("observation", observation_id))
        row = self.db.query_one(
            "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
        )
        if row is None:
            raise KeyError("观测不存在")
        if row["status"] != PENDING_REVIEW:
            raise ValueError("只有待复核观测可以复核")
        # 复核人不能是采集人本人
        if row["collected_by"] == actor.user_id:
            raise FrozenMethodError("观测复核必须由采集人之外的人员完成")
        new_status = ACCEPTED if accept else REJECTED
        self.db.execute(
            "UPDATE observations SET status=?, review_note=?, reviewed_by=?"
            " WHERE observation_id=?",
            (new_status, note, actor.user_id, observation_id),
        )
        self.db.commit()

    def get_observation(self, observation_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
        )
        if row is None:
            raise KeyError(f"观测不存在: {observation_id}")
        return dict(row)

    def accepted_observations(self, parcel_id: str, season: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query_all(
                "SELECT o.* FROM observations o JOIN plots p ON p.plot_id=o.plot_id"
                " WHERE p.parcel_id=? AND o.season=? AND o.status='accepted'"
                " ORDER BY o.observed_on",
                (parcel_id, season),
            )
        ]

    # ---------------------------------------------------------------- 估算结果

    def submit_estimate(
        self,
        actor: Actor,
        *,
        parcel_id: str,
        metric: str,
        unit: str,
        season: str,
        model_version: str,
        value: float,
        basis_observation_ids: list[str],
    ) -> str:
        """提交估算草稿。只允许使用已采纳观测；确认结果不被新模型覆盖。"""
        self.ac.require(actor, "estimate.submit", ("estimate", f"{parcel_id}/{metric}/{season}"))
        if not basis_observation_ids:
            raise ValueError("估算必须基于至少一条观测")
        for obs_id in basis_observation_ids:
            obs = self.get_observation(obs_id)
            if obs["status"] != ACCEPTED:
                raise ValueError(f"观测 {obs_id} 未通过复核，不能作为估算依据")

        # 已确认结果冻结：新模型可以存草稿，但不能顶替确认值
        confirmed = self.db.query_one(
            "SELECT estimate_id, model_version FROM estimates"
            " WHERE parcel_id=? AND metric=? AND season=? AND status='confirmed'",
            (parcel_id, metric, season),
        )
        if confirmed is not None:
            raise ImmutableResultError(
                f"{season} 的 {metric} 已由模型 {confirmed['model_version']} 确认并冻结，"
                "新模型只能另存草稿，不能改写已确认结果"
            )

        existing = self.db.query_one(
            "SELECT estimate_id FROM estimates WHERE parcel_id=? AND metric=? AND season=?"
            " AND model_version=?",
            (parcel_id, metric, season, model_version),
        )
        if existing is not None:
            return existing["estimate_id"]

        estimate_id = _new_id("est")
        self.db.execute(
            "INSERT INTO estimates(estimate_id, parcel_id, metric, unit, season,"
            " model_version, value, basis_observation_ids, status, submitted_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?,?)",
            (
                estimate_id,
                parcel_id,
                metric,
                unit,
                season,
                model_version,
                value,
                dumps(basis_observation_ids),
                actor.user_id,
                now_iso(),
            ),
        )
        self.db.commit()
        return estimate_id

    def confirm_estimate(self, actor: Actor, estimate_id: str) -> None:
        self.ac.require(actor, "estimate.confirm", ("estimate", estimate_id))
        row = self.db.query_one("SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,))
        if row is None:
            raise KeyError("估算不存在")
        if row["status"] == "confirmed":
            raise ImmutableResultError("估算结果已确认，确认操作不可重复执行")
        blocking = self.db.query_one(
            "SELECT model_version FROM estimates WHERE parcel_id=? AND metric=? AND season=?"
            " AND status='confirmed'",
            (row["parcel_id"], row["metric"], row["season"]),
        )
        if blocking is not None:
            raise ImmutableResultError(
                f"该指标季节已有 {blocking['model_version']} 的确认结果，不得再确认其它模型"
            )
        self.db.execute(
            "UPDATE estimates SET status='confirmed', confirmed_by=?, confirmed_at=?"
            " WHERE estimate_id=?",
            (actor.user_id, now_iso(), estimate_id),
        )
        self.db.commit()

    def get_estimate(self, estimate_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM estimates WHERE estimate_id=?", (estimate_id,))
        if row is None:
            raise KeyError("估算不存在")
        data = dict(row)
        data["basis_observation_ids"] = loads(data["basis_observation_ids"])
        return data

    def _require_plan(self, parcel_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM restoration_plans WHERE parcel_id=?", (parcel_id,))
        if row is None:
            raise KeyError(f"地块 {parcel_id} 缺少恢复方案，无法开展监测")
        data = dict(row)
        data["species_mix"] = loads(data["species_mix"])
        data["monitor_seasons"] = loads(data["monitor_seasons"])
        return data

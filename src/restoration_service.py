"""森林景观恢复承诺与社区收益核证服务。

在领域资料（``src/news_context_003.py``）之上提供可执行的核证流程：

- 登记地块边界、权利人和退化基线，项目申报时检查空间重叠与用途冲突；
- 监测样方冻结采集方法与适用季节，已确认结果不被后来的估算模型改写；
- 重复观测保持幂等，异文数据进入复核；
- 受灾地块保留历史投入与失败原因，可进入补植或重做；
- 提交监测的人不能独自批准付款，社区敏感资料仅向授权岗位开放；
- 服务状态可快照并在重启后继续到期监测、待协商申诉和暂停资金任务。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

DOMAIN = "landscape-restoration-commitments"
SNAPSHOT_VERSION = 1

Point = tuple[float, float]
Ring = tuple[Point, ...]


# ---------------------------------------------------------------------------
# 几何：简单多边形的面积与重叠检查
# ---------------------------------------------------------------------------


def _normalize_ring(points: Iterable[Iterable[float]]) -> Ring:
    ring = tuple((float(p[0]), float(p[1])) for p in points)
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError("地块边界至少需要三个顶点")
    if len(set(ring)) != len(ring):
        raise ValueError("地块边界包含重复顶点")
    if abs(_signed_area(ring)) == 0:
        raise ValueError("地块边界面积无效")
    return ring


def _signed_area(ring: Ring) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area(ring: Ring) -> float:
    """返回简单多边形的面积。"""
    return abs(_signed_area(ring))


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    return ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    )


def _on_segment(a: Point, b: Point, p: Point) -> bool:
    """p 是否在线段 ab 上（含端点），调用前已知共线或用于边界排除。"""
    return (
        _orient(a, b, p) == 0
        and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def _strictly_inside(point: Point, ring: Ring) -> bool:
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if _on_segment(a, b, point):
            return False  # 边界上的点不算严格内部
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y):
            cross_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < cross_x:
                inside = not inside
    return inside


def polygons_overlap(first: Ring, second: Ring) -> bool:
    """判断两个简单多边形的内部是否重叠（仅共边或共点不算重叠）。"""
    if set(first) == set(second):
        return True
    for a1, a2 in zip(first, first[1:] + first[:1]):
        for b1, b2 in zip(second, second[1:] + second[:1]):
            if _proper_cross(a1, a2, b1, b2):
                return True
    return any(_strictly_inside(p, second) for p in first) or any(
        _strictly_inside(p, first) for p in second
    )


# ---------------------------------------------------------------------------
# 岗位与操作人
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """岗位，对应领域资料中的参与方。"""

    SECRETARIAT = "secretariat"  # 景观恢复计划秘书处
    COMMUNITY_LIAISON = "community_liaison"  # 社区联络岗位
    IMPLEMENTER = "implementer"  # 实施机构
    MONITOR = "monitor"  # 生态监测团队
    FUNDING_OFFICER = "funding_officer"  # 出资与审核人员
    AUDITOR = "auditor"  # 独立审核


DEFAULT_SENSITIVE_ROLES = frozenset({Role.COMMUNITY_LIAISON, Role.AUDITOR})


@dataclass(frozen=True)
class Actor:
    """执行操作的人，权限按岗位判断。"""

    actor_id: str
    roles: frozenset[Role]

    def has_any(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))


def _require(actor: Actor, *roles: Role) -> None:
    if not actor.has_any(*roles):
        raise PermissionError("当前岗位无权执行该操作")


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RightsHolder:
    """土地权利人；sensitive_detail 仅向授权岗位开放。"""

    holder_id: str
    display_name: str
    right_type: str
    sensitive_detail: str


@dataclass(frozen=True)
class DegradationBaseline:
    """退化基线。"""

    degradation_class: str
    canopy_cover_pct: float
    assessed_on: date
    method: str


class ParcelStatus(str, Enum):
    ACTIVE = "active"
    DISTURBED = "disturbed"
    RECOVERING = "recovering"


@dataclass
class Parcel:
    parcel_id: str
    boundary: Ring
    holders: tuple[RightsHolder, ...]
    baseline: DegradationBaseline
    status: ParcelStatus = ParcelStatus.ACTIVE

    @property
    def area(self) -> float:
        return polygon_area(self.boundary)


@dataclass(frozen=True)
class RestorationPlan:
    """恢复方案。"""

    technique: str
    species: tuple[str, ...]
    target_canopy_pct: float
    starts_on: date
    ends_on: date


@dataclass(frozen=True)
class FundingSource:
    """资金来源。"""

    funder_id: str
    label: str
    amount: float
    currency: str


class CommitmentStatus(str, Enum):
    ACTIVE = "active"
    REJECTED = "rejected"


@dataclass
class Commitment:
    """恢复承诺（项目申报）。"""

    commitment_id: str
    parcel_ids: tuple[str, ...]
    land_use: str
    plan: RestorationPlan
    funding: tuple[FundingSource, ...]
    status: CommitmentStatus
    rejection_reasons: tuple[str, ...] = ()


class ConsultationStatus(str, Enum):
    OPEN = "open"
    AGREED = "agreed"
    APPEALED = "appealed"
    RESOLVED = "resolved"


@dataclass
class Consultation:
    """社区协商记录；summary 属于社区敏感资料。"""

    consultation_id: str
    commitment_id: str
    community: str
    summary: str
    opened_on: date
    status: ConsultationStatus = ConsultationStatus.OPEN


@dataclass(frozen=True)
class WorkBatch:
    """作业批次，构成地块的历史投入。"""

    batch_id: str
    commitment_id: str
    parcel_id: str
    activity: str
    cost: float
    currency: str
    executed_on: date


@dataclass(frozen=True)
class MonitoringProtocol:
    """监测规程：采集方法与适用季节在样方登记时冻结。"""

    method: str
    season: str


@dataclass
class MonitoringPlot:
    plot_id: str
    parcel_id: str
    protocol: MonitoringProtocol
    frequency_days: int
    next_due: date


class ObservationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


@dataclass
class Observation:
    """监测观测；以（样方, 幂等键）标识，确认后不可改写。"""

    plot_id: str
    key: str
    indicator: str
    value: float
    method: str
    season: str
    observed_on: date
    submitted_by: str
    status: ObservationStatus = ObservationStatus.PENDING


@dataclass(frozen=True)
class Estimate:
    """估算模型产出，只作参考，不能改写已确认的观测结果。"""

    plot_id: str
    indicator: str
    value: float
    model_version: str
    created_on: date


class ReviewStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass
class ReviewCase:
    """异文复核案件：同一幂等键提交了不同数据。"""

    case_id: str
    plot_id: str
    key: str
    original: dict[str, Any]
    divergent: dict[str, Any]
    status: ReviewStatus = ReviewStatus.OPEN
    resolution: str = ""


@dataclass(frozen=True)
class Disturbance:
    """火灾、病虫害等灾害事件。"""

    event_id: str
    parcel_id: str
    kind: str
    occurred_on: date
    severity: str


class RecoveryStatus(str, Enum):
    PLANNED = "planned"
    COMPLETED = "completed"


@dataclass
class RecoveryAction:
    """补植或重做；失败原因随行动保留。"""

    action_id: str
    parcel_id: str
    disturbance_id: str
    mode: str
    failure_reason: str
    planned_on: date
    status: RecoveryStatus = RecoveryStatus.PLANNED


@dataclass(frozen=True)
class EvidenceRequirement:
    """付款里程碑的监测证据要求。"""

    indicator: str
    min_confirmed: int


class MilestoneStatus(str, Enum):
    PENDING = "pending"
    PAUSED = "paused"
    RELEASED = "released"


@dataclass
class Milestone:
    milestone_id: str
    commitment_id: str
    stage: str
    amount: float
    currency: str
    requirements: tuple[EvidenceRequirement, ...]
    status: MilestoneStatus = MilestoneStatus.PENDING
    approvals: tuple[str, ...] = ()
    released_on: date | None = None
    released_by: str = ""


def _observation_payload(
    indicator: str,
    value: float,
    method: str,
    season: str,
    observed_on: date,
    submitted_by: str,
) -> dict[str, Any]:
    return {
        "indicator": indicator,
        "value": value,
        "method": method,
        "season": season,
        "observed_on": observed_on.isoformat(),
        "submitted_by": submitted_by,
    }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class RestorationService:
    """承诺与核证服务，全部状态可快照并在重启后恢复。"""

    def __init__(
        self, *, sensitive_roles: Iterable[Role] = DEFAULT_SENSITIVE_ROLES
    ) -> None:
        self._sensitive_roles = frozenset(sensitive_roles)
        if not self._sensitive_roles:
            raise ValueError("敏感资料授权岗位不能为空")
        self._parcels: dict[str, Parcel] = {}
        self._commitments: dict[str, Commitment] = {}
        self._consultations: dict[str, Consultation] = {}
        self._batches: dict[str, WorkBatch] = {}
        self._plots: dict[str, MonitoringPlot] = {}
        self._observations: dict[tuple[str, str], Observation] = {}
        self._estimates: list[Estimate] = []
        self._reviews: dict[str, ReviewCase] = {}
        self._disturbances: dict[str, Disturbance] = {}
        self._recoveries: dict[str, RecoveryAction] = {}
        self._milestones: dict[str, Milestone] = {}
        self._review_seq = 0

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _lookup(mapping: dict[Any, Any], key: Any, message: str) -> Any:
        try:
            return mapping[key]
        except KeyError:
            raise ValueError(message) from None

    def _require_sensitive_access(self, actor: Actor) -> None:
        if not actor.roles.intersection(self._sensitive_roles):
            raise PermissionError("社区敏感资料仅向授权岗位开放")

    def _commitment_plot_ids(self, commitment: Commitment) -> set[str]:
        return {
            plot.plot_id
            for plot in self._plots.values()
            if plot.parcel_id in commitment.parcel_ids
        }

    def _commitment_submitters(self, commitment_id: str) -> set[str]:
        commitment = self._lookup(self._commitments, commitment_id, "承诺不存在")
        plot_ids = self._commitment_plot_ids(commitment)
        return {
            observation.submitted_by
            for (plot_id, _), observation in self._observations.items()
            if plot_id in plot_ids
        }

    def _confirmed_evidence(
        self, commitment: Commitment, indicator: str
    ) -> list[Observation]:
        plot_ids = self._commitment_plot_ids(commitment)
        return [
            observation
            for (plot_id, _), observation in self._observations.items()
            if plot_id in plot_ids
            and observation.indicator == indicator
            and observation.status is ObservationStatus.CONFIRMED
        ]

    # -- 地块与权利基线 -----------------------------------------------------

    def register_parcel(
        self,
        actor: Actor,
        parcel_id: str,
        boundary: Iterable[Iterable[float]],
        holders: Iterable[RightsHolder],
        baseline: DegradationBaseline,
    ) -> Parcel:
        """登记地块边界、权利人和退化基线。"""
        _require(actor, Role.SECRETARIAT)
        if parcel_id in self._parcels:
            raise ValueError("地块编号已存在")
        ring = _normalize_ring(boundary)
        holders = tuple(holders)
        if not holders:
            raise ValueError("地块至少登记一名权利人")
        if not 0.0 <= baseline.canopy_cover_pct <= 100.0:
            raise ValueError("郁闭度必须在0到100之间")
        parcel = Parcel(
            parcel_id=parcel_id, boundary=ring, holders=holders, baseline=baseline
        )
        self._parcels[parcel_id] = parcel
        return parcel

    # -- 承诺申报 -----------------------------------------------------------

    def submit_application(
        self,
        actor: Actor,
        commitment_id: str,
        parcel_ids: Iterable[str],
        land_use: str,
        plan: RestorationPlan,
        funding: Iterable[FundingSource],
    ) -> Commitment:
        """申报恢复承诺，检查空间重叠与用途冲突，防止重复计算。"""
        _require(actor, Role.SECRETARIAT)
        if commitment_id in self._commitments:
            raise ValueError("承诺编号已存在")
        parcel_ids = tuple(parcel_ids)
        if not parcel_ids:
            raise ValueError("承诺必须包含至少一个地块")
        if len(set(parcel_ids)) != len(parcel_ids):
            raise ValueError("承诺包含重复地块")
        if not land_use.strip():
            raise ValueError("承诺必须声明土地用途")
        funding = tuple(funding)
        if not funding:
            raise ValueError("承诺必须登记资金来源")
        if plan.ends_on <= plan.starts_on:
            raise ValueError("恢复方案期限无效")
        parcels = [
            self._lookup(self._parcels, parcel_id, "地块不存在")
            for parcel_id in parcel_ids
        ]
        reasons: list[str] = []
        for other in self._commitments.values():
            if other.status is not CommitmentStatus.ACTIVE:
                continue
            for parcel in parcels:
                for other_parcel_id in other.parcel_ids:
                    other_parcel = self._parcels[other_parcel_id]
                    if not polygons_overlap(parcel.boundary, other_parcel.boundary):
                        continue
                    if other.land_use == land_use:
                        reasons.append(
                            f"地块{parcel.parcel_id}与承诺{other.commitment_id}的"
                            f"地块{other_parcel_id}空间重叠，用途相同构成重复申报"
                        )
                    else:
                        reasons.append(
                            f"地块{parcel.parcel_id}与承诺{other.commitment_id}的"
                            f"地块{other_parcel_id}空间重叠，用途冲突："
                            f"{land_use}与{other.land_use}不兼容"
                        )
        status = CommitmentStatus.REJECTED if reasons else CommitmentStatus.ACTIVE
        commitment = Commitment(
            commitment_id=commitment_id,
            parcel_ids=parcel_ids,
            land_use=land_use,
            plan=plan,
            funding=funding,
            status=status,
            rejection_reasons=tuple(reasons),
        )
        self._commitments[commitment_id] = commitment
        return commitment

    # -- 社区协商 -----------------------------------------------------------

    def open_consultation(
        self,
        actor: Actor,
        consultation_id: str,
        commitment_id: str,
        community: str,
        summary: str,
        opened_on: date,
    ) -> Consultation:
        _require(actor, Role.COMMUNITY_LIAISON)
        self._lookup(self._commitments, commitment_id, "承诺不存在")
        if consultation_id in self._consultations:
            raise ValueError("协商记录编号已存在")
        record = Consultation(
            consultation_id=consultation_id,
            commitment_id=commitment_id,
            community=community,
            summary=summary,
            opened_on=opened_on,
        )
        self._consultations[consultation_id] = record
        return record

    def _transition_consultation(
        self,
        actor: Actor,
        consultation_id: str,
        allowed: set[ConsultationStatus],
        target: ConsultationStatus,
    ) -> Consultation:
        _require(actor, Role.COMMUNITY_LIAISON)
        record = self._lookup(self._consultations, consultation_id, "协商记录不存在")
        if record.status not in allowed:
            raise ValueError("协商状态不允许该操作")
        record.status = target
        return record

    def agree_consultation(self, actor: Actor, consultation_id: str) -> Consultation:
        return self._transition_consultation(
            actor, consultation_id, {ConsultationStatus.OPEN}, ConsultationStatus.AGREED
        )

    def appeal_consultation(self, actor: Actor, consultation_id: str) -> Consultation:
        return self._transition_consultation(
            actor,
            consultation_id,
            {ConsultationStatus.OPEN, ConsultationStatus.AGREED},
            ConsultationStatus.APPEALED,
        )

    def resolve_appeal(self, actor: Actor, consultation_id: str) -> Consultation:
        return self._transition_consultation(
            actor,
            consultation_id,
            {ConsultationStatus.APPEALED},
            ConsultationStatus.RESOLVED,
        )

    # -- 作业批次 -----------------------------------------------------------

    def record_work_batch(
        self,
        actor: Actor,
        batch_id: str,
        commitment_id: str,
        parcel_id: str,
        activity: str,
        cost: float,
        currency: str,
        executed_on: date,
    ) -> WorkBatch:
        """登记作业批次；批次一旦登记即成为不可删除的历史投入。"""
        _require(actor, Role.IMPLEMENTER, Role.SECRETARIAT)
        commitment = self._lookup(self._commitments, commitment_id, "承诺不存在")
        if commitment.status is not CommitmentStatus.ACTIVE:
            raise ValueError("承诺未处于执行状态")
        if parcel_id not in commitment.parcel_ids:
            raise ValueError("作业批次地块不属于该承诺")
        if batch_id in self._batches:
            raise ValueError("作业批次编号已存在")
        if cost < 0:
            raise ValueError("作业成本不能为负")
        batch = WorkBatch(
            batch_id=batch_id,
            commitment_id=commitment_id,
            parcel_id=parcel_id,
            activity=activity,
            cost=float(cost),
            currency=currency,
            executed_on=executed_on,
        )
        self._batches[batch_id] = batch
        return batch

    # -- 监测样方与观测 -----------------------------------------------------

    def register_plot(
        self,
        actor: Actor,
        plot_id: str,
        parcel_id: str,
        *,
        method: str,
        season: str,
        frequency_days: int,
        registered_on: date,
    ) -> MonitoringPlot:
        """登记监测样方并冻结采集方法与适用季节。"""
        _require(actor, Role.SECRETARIAT)
        self._lookup(self._parcels, parcel_id, "地块不存在")
        if plot_id in self._plots:
            raise ValueError("样方编号已存在")
        if frequency_days <= 0:
            raise ValueError("监测频次必须为正")
        plot = MonitoringPlot(
            plot_id=plot_id,
            parcel_id=parcel_id,
            protocol=MonitoringProtocol(method=method, season=season),
            frequency_days=int(frequency_days),
            next_due=registered_on,
        )
        self._plots[plot_id] = plot
        return plot

    def record_observation(
        self,
        actor: Actor,
        plot_id: str,
        key: str,
        indicator: str,
        value: float,
        method: str,
        season: str,
        observed_on: date,
    ) -> Observation | ReviewCase:
        """记录观测：重复提交幂等返回原记录，异文数据进入复核。"""
        _require(actor, Role.MONITOR)
        plot = self._lookup(self._plots, plot_id, "监测样方不存在")
        if method != plot.protocol.method or season != plot.protocol.season:
            raise ValueError("采集方法或适用季节与冻结的监测规程不一致")
        value = float(value)
        existing = self._observations.get((plot_id, key))
        if existing is None:
            observation = Observation(
                plot_id=plot_id,
                key=key,
                indicator=indicator,
                value=value,
                method=method,
                season=season,
                observed_on=observed_on,
                submitted_by=actor.actor_id,
            )
            self._observations[(plot_id, key)] = observation
            return observation
        incoming = _observation_payload(
            indicator, value, method, season, observed_on, actor.actor_id
        )
        original = _observation_payload(
            existing.indicator,
            existing.value,
            existing.method,
            existing.season,
            existing.observed_on,
            existing.submitted_by,
        )
        if incoming == original:
            return existing
        self._review_seq += 1
        case = ReviewCase(
            case_id=f"review-{self._review_seq}",
            plot_id=plot_id,
            key=key,
            original=original,
            divergent=incoming,
        )
        self._reviews[case.case_id] = case
        return case

    def confirm_observation(self, actor: Actor, plot_id: str, key: str) -> Observation:
        """确认观测；提交人不能确认自己的数据，确认后结果不可改写。"""
        _require(actor, Role.SECRETARIAT, Role.AUDITOR)
        observation = self._lookup(
            self._observations, (plot_id, key), "观测记录不存在"
        )
        if observation.status is ObservationStatus.CONFIRMED:
            raise ValueError("观测记录已确认")
        if observation.submitted_by == actor.actor_id:
            raise PermissionError("观测提交人不能确认自己的观测数据")
        observation.status = ObservationStatus.CONFIRMED
        plot = self._plots[plot_id]
        due = observation.observed_on + timedelta(days=plot.frequency_days)
        if due > plot.next_due:
            plot.next_due = due
        return observation

    def resolve_review(self, actor: Actor, case_id: str, keep: str) -> ReviewCase:
        """复核异文：保留原数据，或在原数据未确认时采用异文。"""
        _require(actor, Role.SECRETARIAT, Role.AUDITOR)
        case = self._lookup(self._reviews, case_id, "复核案件不存在")
        if case.status is ReviewStatus.RESOLVED:
            raise ValueError("复核案件已结案")
        observation = self._observations[(case.plot_id, case.key)]
        if keep == "original":
            case.resolution = "保留原数据"
        elif keep == "divergent":
            if observation.status is ObservationStatus.CONFIRMED:
                raise ValueError("已确认的结果不能被改写")
            divergent = case.divergent
            self._observations[(case.plot_id, case.key)] = Observation(
                plot_id=case.plot_id,
                key=case.key,
                indicator=divergent["indicator"],
                value=divergent["value"],
                method=divergent["method"],
                season=divergent["season"],
                observed_on=date.fromisoformat(divergent["observed_on"]),
                submitted_by=divergent["submitted_by"],
            )
            case.resolution = "采用异文数据"
        else:
            raise ValueError("未知的复核结论")
        case.status = ReviewStatus.RESOLVED
        return case

    def record_estimate(
        self,
        actor: Actor,
        plot_id: str,
        indicator: str,
        value: float,
        model_version: str,
        created_on: date,
    ) -> Estimate:
        """登记估算模型产出；估算与已确认观测分开存放，不能改写观测。"""
        _require(actor, Role.MONITOR, Role.SECRETARIAT)
        self._lookup(self._plots, plot_id, "监测样方不存在")
        if not model_version.strip():
            raise ValueError("估算必须标注模型版本")
        estimate = Estimate(
            plot_id=plot_id,
            indicator=indicator,
            value=float(value),
            model_version=model_version,
            created_on=created_on,
        )
        self._estimates.append(estimate)
        return estimate

    # -- 灾害与补植 ---------------------------------------------------------

    def report_disturbance(
        self,
        actor: Actor,
        event_id: str,
        parcel_id: str,
        kind: str,
        occurred_on: date,
        severity: str,
    ) -> Disturbance:
        """报告火灾或病虫害；受灾地块的相关付款里程碑自动暂停。"""
        _require(actor, Role.MONITOR, Role.SECRETARIAT)
        parcel = self._lookup(self._parcels, parcel_id, "地块不存在")
        if event_id in self._disturbances:
            raise ValueError("灾害事件编号已存在")
        event = Disturbance(
            event_id=event_id,
            parcel_id=parcel_id,
            kind=kind,
            occurred_on=occurred_on,
            severity=severity,
        )
        self._disturbances[event_id] = event
        parcel.status = ParcelStatus.DISTURBED
        for milestone in self._milestones.values():
            if milestone.status is not MilestoneStatus.PENDING:
                continue
            commitment = self._commitments[milestone.commitment_id]
            if parcel_id in commitment.parcel_ids:
                milestone.status = MilestoneStatus.PAUSED
        return event

    def plan_recovery(
        self,
        actor: Actor,
        action_id: str,
        parcel_id: str,
        disturbance_id: str,
        mode: str,
        failure_reason: str,
        planned_on: date,
    ) -> RecoveryAction:
        """为受灾地块安排补植或重做，失败原因随行动保留。"""
        _require(actor, Role.SECRETARIAT)
        parcel = self._lookup(self._parcels, parcel_id, "地块不存在")
        disturbance = self._lookup(self._disturbances, disturbance_id, "灾害事件不存在")
        if parcel.status is not ParcelStatus.DISTURBED:
            raise ValueError("地块未处于受灾状态")
        if disturbance.parcel_id != parcel_id:
            raise ValueError("灾害事件不属于该地块")
        if mode not in ("replant", "redo"):
            raise ValueError("恢复方式必须是补植或重做")
        if not failure_reason.strip():
            raise ValueError("必须记录失败原因")
        if action_id in self._recoveries:
            raise ValueError("恢复行动编号已存在")
        action = RecoveryAction(
            action_id=action_id,
            parcel_id=parcel_id,
            disturbance_id=disturbance_id,
            mode=mode,
            failure_reason=failure_reason,
            planned_on=planned_on,
        )
        self._recoveries[action_id] = action
        parcel.status = ParcelStatus.RECOVERING
        return action

    def complete_recovery(self, actor: Actor, action_id: str) -> RecoveryAction:
        _require(actor, Role.IMPLEMENTER, Role.SECRETARIAT)
        action = self._lookup(self._recoveries, action_id, "恢复行动不存在")
        if action.status is RecoveryStatus.COMPLETED:
            raise ValueError("恢复行动已完成")
        action.status = RecoveryStatus.COMPLETED
        self._parcels[action.parcel_id].status = ParcelStatus.ACTIVE
        return action

    # -- 付款里程碑 ---------------------------------------------------------

    def add_milestone(
        self,
        actor: Actor,
        milestone_id: str,
        commitment_id: str,
        stage: str,
        amount: float,
        currency: str,
        requirements: Iterable[EvidenceRequirement],
    ) -> Milestone:
        _require(actor, Role.SECRETARIAT)
        commitment = self._lookup(self._commitments, commitment_id, "承诺不存在")
        if commitment.status is not CommitmentStatus.ACTIVE:
            raise ValueError("承诺未处于执行状态")
        if milestone_id in self._milestones:
            raise ValueError("里程碑编号已存在")
        if amount <= 0:
            raise ValueError("付款金额必须为正")
        requirements = tuple(requirements)
        if not requirements:
            raise ValueError("里程碑必须声明监测证据要求")
        if any(requirement.min_confirmed < 1 for requirement in requirements):
            raise ValueError("证据要求至少一条确认观测")
        milestone = Milestone(
            milestone_id=milestone_id,
            commitment_id=commitment_id,
            stage=stage,
            amount=float(amount),
            currency=currency,
            requirements=requirements,
        )
        self._milestones[milestone_id] = milestone
        return milestone

    def approve_milestone(self, actor: Actor, milestone_id: str) -> Milestone:
        """出资审核岗位批准里程碑；提交监测的人不能批准付款。"""
        _require(actor, Role.FUNDING_OFFICER)
        milestone = self._lookup(self._milestones, milestone_id, "付款里程碑不存在")
        if milestone.status is MilestoneStatus.PAUSED:
            raise ValueError("资金任务已暂停，待恢复后才能审批")
        if milestone.status is MilestoneStatus.RELEASED:
            raise ValueError("里程碑已完成付款")
        if actor.actor_id in self._commitment_submitters(milestone.commitment_id):
            raise PermissionError("提交监测的人不能批准付款")
        if actor.actor_id not in milestone.approvals:
            milestone.approvals = milestone.approvals + (actor.actor_id,)
        return milestone

    def release_milestone(
        self, actor: Actor, milestone_id: str, released_on: date
    ) -> Milestone:
        """放行付款：证据达标、至少两名出资审核批准、审批人与监测提交人无关。"""
        _require(actor, Role.FUNDING_OFFICER)
        milestone = self._lookup(self._milestones, milestone_id, "付款里程碑不存在")
        if milestone.status is MilestoneStatus.PAUSED:
            raise ValueError("资金任务已暂停，待恢复后才能放行")
        if milestone.status is MilestoneStatus.RELEASED:
            raise ValueError("里程碑已完成付款")
        submitters = self._commitment_submitters(milestone.commitment_id)
        if actor.actor_id in submitters:
            raise PermissionError("提交监测的人不能批准付款")
        approvers = set(milestone.approvals)
        if len(approvers) < 2:
            raise ValueError("付款里程碑需要至少两名出资审核人员批准")
        if approvers.intersection(submitters):
            raise PermissionError("提交监测的人不能批准付款")
        commitment = self._commitments[milestone.commitment_id]
        for requirement in milestone.requirements:
            evidence = self._confirmed_evidence(commitment, requirement.indicator)
            if len(evidence) < requirement.min_confirmed:
                raise ValueError(f"监测证据不足：{requirement.indicator}")
        milestone.status = MilestoneStatus.RELEASED
        milestone.released_on = released_on
        milestone.released_by = actor.actor_id
        return milestone

    def resume_milestone(self, actor: Actor, milestone_id: str) -> Milestone:
        """恢复暂停的资金任务：相关灾害须已完成补植或重做。"""
        _require(actor, Role.SECRETARIAT)
        milestone = self._lookup(self._milestones, milestone_id, "付款里程碑不存在")
        if milestone.status is not MilestoneStatus.PAUSED:
            raise ValueError("里程碑未处于暂停状态")
        commitment = self._commitments[milestone.commitment_id]
        for parcel_id in commitment.parcel_ids:
            for event in self._disturbances.values():
                if event.parcel_id != parcel_id:
                    continue
                recovered = any(
                    action.disturbance_id == event.event_id
                    and action.status is RecoveryStatus.COMPLETED
                    for action in self._recoveries.values()
                )
                if not recovered:
                    raise ValueError("受灾地块尚未完成补植或重做")
        milestone.status = MilestoneStatus.PENDING
        return milestone

    # -- 查询与敏感资料 -----------------------------------------------------

    def get_parcel(self, parcel_id: str) -> Parcel:
        return self._lookup(self._parcels, parcel_id, "地块不存在")

    def get_commitment(self, commitment_id: str) -> Commitment:
        return self._lookup(self._commitments, commitment_id, "承诺不存在")

    def get_plot(self, plot_id: str) -> MonitoringPlot:
        return self._lookup(self._plots, plot_id, "监测样方不存在")

    def get_observation(self, plot_id: str, key: str) -> Observation:
        return self._lookup(self._observations, (plot_id, key), "观测记录不存在")

    def get_milestone(self, milestone_id: str) -> Milestone:
        return self._lookup(self._milestones, milestone_id, "付款里程碑不存在")

    def get_disturbance(self, event_id: str) -> Disturbance:
        return self._lookup(self._disturbances, event_id, "灾害事件不存在")

    def get_recovery(self, action_id: str) -> RecoveryAction:
        return self._lookup(self._recoveries, action_id, "恢复行动不存在")

    def get_review(self, case_id: str) -> ReviewCase:
        return self._lookup(self._reviews, case_id, "复核案件不存在")

    def consultation_status(self, consultation_id: str) -> ConsultationStatus:
        record = self._lookup(self._consultations, consultation_id, "协商记录不存在")
        return record.status

    def open_reviews(self) -> tuple[ReviewCase, ...]:
        return tuple(
            case
            for case in self._reviews.values()
            if case.status is ReviewStatus.OPEN
        )

    def confirmed_observations(
        self, plot_id: str, indicator: str
    ) -> tuple[Observation, ...]:
        return tuple(
            observation
            for (pid, _), observation in self._observations.items()
            if pid == plot_id
            and observation.indicator == indicator
            and observation.status is ObservationStatus.CONFIRMED
        )

    def estimates(self, plot_id: str) -> tuple[Estimate, ...]:
        return tuple(
            estimate for estimate in self._estimates if estimate.plot_id == plot_id
        )

    def rights_holders(self, actor: Actor, parcel_id: str) -> tuple[RightsHolder, ...]:
        """读取权利人完整资料（含敏感信息），仅授权岗位可用。"""
        self._require_sensitive_access(actor)
        return self._lookup(self._parcels, parcel_id, "地块不存在").holders

    def public_rights(self, parcel_id: str) -> tuple[dict[str, str], ...]:
        """权利人公开视图，不含敏感信息。"""
        parcel = self._lookup(self._parcels, parcel_id, "地块不存在")
        return tuple(
            {
                "holder_id": holder.holder_id,
                "display_name": holder.display_name,
                "right_type": holder.right_type,
            }
            for holder in parcel.holders
        )

    def consultation_detail(self, actor: Actor, consultation_id: str) -> Consultation:
        """读取协商纪要（社区敏感资料），仅授权岗位可用。"""
        self._require_sensitive_access(actor)
        return self._lookup(self._consultations, consultation_id, "协商记录不存在")

    def parcel_investment(self, parcel_id: str) -> dict[str, Any]:
        """地块的历史投入：全部作业批次与按币种汇总，灾害后仍然保留。"""
        self._lookup(self._parcels, parcel_id, "地块不存在")
        batches = tuple(
            batch for batch in self._batches.values() if batch.parcel_id == parcel_id
        )
        totals: dict[str, float] = {}
        for batch in batches:
            totals[batch.currency] = totals.get(batch.currency, 0.0) + batch.cost
        return {"batches": batches, "total_by_currency": totals}

    # -- 到期任务与审计 -----------------------------------------------------

    def pending_tasks(self, today: date) -> dict[str, list[str]]:
        """列出需要继续的任务：到期监测、待协商申诉、暂停资金。"""
        return {
            "due_monitoring": sorted(
                plot.plot_id
                for plot in self._plots.values()
                if plot.next_due <= today
            ),
            "pending_appeals": sorted(
                record.consultation_id
                for record in self._consultations.values()
                if record.status is ConsultationStatus.APPEALED
            ),
            "paused_funding": sorted(
                milestone.milestone_id
                for milestone in self._milestones.values()
                if milestone.status is MilestoneStatus.PAUSED
            ),
        }

    def explain_payment(self, actor: Actor, milestone_id: str) -> dict[str, Any]:
        """审计解释：一笔恢复资金对应哪块土地、哪些证据和哪些社区权益。"""
        _require(actor, Role.AUDITOR, Role.FUNDING_OFFICER)
        milestone = self._lookup(self._milestones, milestone_id, "付款里程碑不存在")
        commitment = self._commitments[milestone.commitment_id]
        sensitive = bool(actor.roles.intersection(self._sensitive_roles))
        parcels = []
        for parcel_id in commitment.parcel_ids:
            parcel = self._parcels[parcel_id]
            holders = []
            for holder in parcel.holders:
                view = {
                    "holder_id": holder.holder_id,
                    "display_name": holder.display_name,
                    "right_type": holder.right_type,
                }
                if sensitive:
                    view["sensitive_detail"] = holder.sensitive_detail
                holders.append(view)
            parcels.append(
                {
                    "parcel_id": parcel.parcel_id,
                    "area": parcel.area,
                    "status": parcel.status.value,
                    "rights_holders": holders,
                }
            )
        indicators = {requirement.indicator for requirement in milestone.requirements}
        plot_ids = self._commitment_plot_ids(commitment)
        evidence = [
            {
                "plot_id": observation.plot_id,
                "key": observation.key,
                "indicator": observation.indicator,
                "value": observation.value,
                "observed_on": observation.observed_on.isoformat(),
                "submitted_by": observation.submitted_by,
            }
            for (plot_id, _), observation in sorted(self._observations.items())
            if plot_id in plot_ids
            and observation.indicator in indicators
            and observation.status is ObservationStatus.CONFIRMED
        ]
        consultations = []
        for record in self._consultations.values():
            if record.commitment_id != commitment.commitment_id:
                continue
            view = {
                "consultation_id": record.consultation_id,
                "community": record.community,
                "status": record.status.value,
            }
            if sensitive:
                view["summary"] = record.summary
            consultations.append(view)
        batches = [
            batch
            for batch in self._batches.values()
            if batch.commitment_id == commitment.commitment_id
        ]
        totals: dict[str, float] = {}
        for batch in batches:
            totals[batch.currency] = totals.get(batch.currency, 0.0) + batch.cost
        disturbances = [
            event
            for event in self._disturbances.values()
            if event.parcel_id in commitment.parcel_ids
        ]
        recoveries = [
            action
            for action in self._recoveries.values()
            if action.parcel_id in commitment.parcel_ids
        ]
        return {
            "milestone_id": milestone.milestone_id,
            "commitment_id": commitment.commitment_id,
            "stage": milestone.stage,
            "amount": milestone.amount,
            "currency": milestone.currency,
            "status": milestone.status.value,
            "approvals": list(milestone.approvals),
            "released_by": milestone.released_by,
            "released_on": (
                milestone.released_on.isoformat() if milestone.released_on else None
            ),
            "land_use": commitment.land_use,
            "funding": [
                {
                    "funder_id": source.funder_id,
                    "label": source.label,
                    "amount": source.amount,
                    "currency": source.currency,
                }
                for source in commitment.funding
            ],
            "parcels": parcels,
            "evidence": evidence,
            "consultations": consultations,
            "investment": {
                "batch_count": len(batches),
                "total_by_currency": totals,
            },
            "disturbances": [
                {
                    "event_id": event.event_id,
                    "parcel_id": event.parcel_id,
                    "kind": event.kind,
                    "occurred_on": event.occurred_on.isoformat(),
                }
                for event in disturbances
            ],
            "recoveries": [
                {
                    "action_id": action.action_id,
                    "parcel_id": action.parcel_id,
                    "mode": action.mode,
                    "failure_reason": action.failure_reason,
                    "status": action.status.value,
                }
                for action in recoveries
            ],
        }

    # -- 快照与恢复 ---------------------------------------------------------

    def snapshot(self, path: Path | str) -> None:
        """把全部状态写入快照文件，供系统重启后恢复。"""
        payload = {
            "domain": DOMAIN,
            "snapshot_version": SNAPSHOT_VERSION,
            "state": self._state_dict(),
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def restore(cls, path: Path | str) -> "RestorationService":
        """从快照恢复服务，恢复后到期监测、申诉与暂停资金任务保持连续。"""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("domain") != DOMAIN:
            raise ValueError("领域标识不一致")
        if payload.get("snapshot_version") != SNAPSHOT_VERSION:
            raise ValueError("快照版本不受支持")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ValueError("快照内容不完整")
        try:
            service = cls(
                sensitive_roles=frozenset(
                    Role(item) for item in state["sensitive_roles"]
                )
            )
            service._review_seq = int(state["review_seq"])
            for item in state["parcels"]:
                baseline = item["baseline"]
                parcel = Parcel(
                    parcel_id=item["parcel_id"],
                    boundary=_normalize_ring(item["boundary"]),
                    holders=tuple(
                        RightsHolder(**holder) for holder in item["holders"]
                    ),
                    baseline=DegradationBaseline(
                        degradation_class=baseline["degradation_class"],
                        canopy_cover_pct=baseline["canopy_cover_pct"],
                        assessed_on=date.fromisoformat(baseline["assessed_on"]),
                        method=baseline["method"],
                    ),
                    status=ParcelStatus(item["status"]),
                )
                service._parcels[parcel.parcel_id] = parcel
            for item in state["commitments"]:
                plan = item["plan"]
                commitment = Commitment(
                    commitment_id=item["commitment_id"],
                    parcel_ids=tuple(item["parcel_ids"]),
                    land_use=item["land_use"],
                    plan=RestorationPlan(
                        technique=plan["technique"],
                        species=tuple(plan["species"]),
                        target_canopy_pct=plan["target_canopy_pct"],
                        starts_on=date.fromisoformat(plan["starts_on"]),
                        ends_on=date.fromisoformat(plan["ends_on"]),
                    ),
                    funding=tuple(
                        FundingSource(**source) for source in item["funding"]
                    ),
                    status=CommitmentStatus(item["status"]),
                    rejection_reasons=tuple(item["rejection_reasons"]),
                )
                service._commitments[commitment.commitment_id] = commitment
            for item in state["consultations"]:
                record = Consultation(
                    consultation_id=item["consultation_id"],
                    commitment_id=item["commitment_id"],
                    community=item["community"],
                    summary=item["summary"],
                    opened_on=date.fromisoformat(item["opened_on"]),
                    status=ConsultationStatus(item["status"]),
                )
                service._consultations[record.consultation_id] = record
            for item in state["batches"]:
                batch = WorkBatch(
                    batch_id=item["batch_id"],
                    commitment_id=item["commitment_id"],
                    parcel_id=item["parcel_id"],
                    activity=item["activity"],
                    cost=item["cost"],
                    currency=item["currency"],
                    executed_on=date.fromisoformat(item["executed_on"]),
                )
                service._batches[batch.batch_id] = batch
            for item in state["plots"]:
                plot = MonitoringPlot(
                    plot_id=item["plot_id"],
                    parcel_id=item["parcel_id"],
                    protocol=MonitoringProtocol(
                        method=item["protocol"]["method"],
                        season=item["protocol"]["season"],
                    ),
                    frequency_days=item["frequency_days"],
                    next_due=date.fromisoformat(item["next_due"]),
                )
                service._plots[plot.plot_id] = plot
            for item in state["observations"]:
                observation = Observation(
                    plot_id=item["plot_id"],
                    key=item["key"],
                    indicator=item["indicator"],
                    value=item["value"],
                    method=item["method"],
                    season=item["season"],
                    observed_on=date.fromisoformat(item["observed_on"]),
                    submitted_by=item["submitted_by"],
                    status=ObservationStatus(item["status"]),
                )
                service._observations[(observation.plot_id, observation.key)] = (
                    observation
                )
            for item in state["estimates"]:
                service._estimates.append(
                    Estimate(
                        plot_id=item["plot_id"],
                        indicator=item["indicator"],
                        value=item["value"],
                        model_version=item["model_version"],
                        created_on=date.fromisoformat(item["created_on"]),
                    )
                )
            for item in state["reviews"]:
                case = ReviewCase(
                    case_id=item["case_id"],
                    plot_id=item["plot_id"],
                    key=item["key"],
                    original=item["original"],
                    divergent=item["divergent"],
                    status=ReviewStatus(item["status"]),
                    resolution=item["resolution"],
                )
                service._reviews[case.case_id] = case
            for item in state["disturbances"]:
                event = Disturbance(
                    event_id=item["event_id"],
                    parcel_id=item["parcel_id"],
                    kind=item["kind"],
                    occurred_on=date.fromisoformat(item["occurred_on"]),
                    severity=item["severity"],
                )
                service._disturbances[event.event_id] = event
            for item in state["recoveries"]:
                action = RecoveryAction(
                    action_id=item["action_id"],
                    parcel_id=item["parcel_id"],
                    disturbance_id=item["disturbance_id"],
                    mode=item["mode"],
                    failure_reason=item["failure_reason"],
                    planned_on=date.fromisoformat(item["planned_on"]),
                    status=RecoveryStatus(item["status"]),
                )
                service._recoveries[action.action_id] = action
            for item in state["milestones"]:
                milestone = Milestone(
                    milestone_id=item["milestone_id"],
                    commitment_id=item["commitment_id"],
                    stage=item["stage"],
                    amount=item["amount"],
                    currency=item["currency"],
                    requirements=tuple(
                        EvidenceRequirement(**requirement)
                        for requirement in item["requirements"]
                    ),
                    status=MilestoneStatus(item["status"]),
                    approvals=tuple(item["approvals"]),
                    released_on=(
                        date.fromisoformat(item["released_on"])
                        if item["released_on"]
                        else None
                    ),
                    released_by=item["released_by"],
                )
                service._milestones[milestone.milestone_id] = milestone
        except (KeyError, TypeError) as error:
            raise ValueError("快照内容不完整") from error
        return service

    def _state_dict(self) -> dict[str, Any]:
        return {
            "sensitive_roles": sorted(
                role.value for role in self._sensitive_roles
            ),
            "review_seq": self._review_seq,
            "parcels": [
                {
                    "parcel_id": parcel.parcel_id,
                    "boundary": [[x, y] for x, y in parcel.boundary],
                    "holders": [
                        {
                            "holder_id": holder.holder_id,
                            "display_name": holder.display_name,
                            "right_type": holder.right_type,
                            "sensitive_detail": holder.sensitive_detail,
                        }
                        for holder in parcel.holders
                    ],
                    "baseline": {
                        "degradation_class": parcel.baseline.degradation_class,
                        "canopy_cover_pct": parcel.baseline.canopy_cover_pct,
                        "assessed_on": parcel.baseline.assessed_on.isoformat(),
                        "method": parcel.baseline.method,
                    },
                    "status": parcel.status.value,
                }
                for parcel in self._parcels.values()
            ],
            "commitments": [
                {
                    "commitment_id": commitment.commitment_id,
                    "parcel_ids": list(commitment.parcel_ids),
                    "land_use": commitment.land_use,
                    "plan": {
                        "technique": commitment.plan.technique,
                        "species": list(commitment.plan.species),
                        "target_canopy_pct": commitment.plan.target_canopy_pct,
                        "starts_on": commitment.plan.starts_on.isoformat(),
                        "ends_on": commitment.plan.ends_on.isoformat(),
                    },
                    "funding": [
                        {
                            "funder_id": source.funder_id,
                            "label": source.label,
                            "amount": source.amount,
                            "currency": source.currency,
                        }
                        for source in commitment.funding
                    ],
                    "status": commitment.status.value,
                    "rejection_reasons": list(commitment.rejection_reasons),
                }
                for commitment in self._commitments.values()
            ],
            "consultations": [
                {
                    "consultation_id": record.consultation_id,
                    "commitment_id": record.commitment_id,
                    "community": record.community,
                    "summary": record.summary,
                    "opened_on": record.opened_on.isoformat(),
                    "status": record.status.value,
                }
                for record in self._consultations.values()
            ],
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "commitment_id": batch.commitment_id,
                    "parcel_id": batch.parcel_id,
                    "activity": batch.activity,
                    "cost": batch.cost,
                    "currency": batch.currency,
                    "executed_on": batch.executed_on.isoformat(),
                }
                for batch in self._batches.values()
            ],
            "plots": [
                {
                    "plot_id": plot.plot_id,
                    "parcel_id": plot.parcel_id,
                    "protocol": {
                        "method": plot.protocol.method,
                        "season": plot.protocol.season,
                    },
                    "frequency_days": plot.frequency_days,
                    "next_due": plot.next_due.isoformat(),
                }
                for plot in self._plots.values()
            ],
            "observations": [
                {
                    "plot_id": observation.plot_id,
                    "key": observation.key,
                    "indicator": observation.indicator,
                    "value": observation.value,
                    "method": observation.method,
                    "season": observation.season,
                    "observed_on": observation.observed_on.isoformat(),
                    "submitted_by": observation.submitted_by,
                    "status": observation.status.value,
                }
                for observation in self._observations.values()
            ],
            "estimates": [
                {
                    "plot_id": estimate.plot_id,
                    "indicator": estimate.indicator,
                    "value": estimate.value,
                    "model_version": estimate.model_version,
                    "created_on": estimate.created_on.isoformat(),
                }
                for estimate in self._estimates
            ],
            "reviews": [
                {
                    "case_id": case.case_id,
                    "plot_id": case.plot_id,
                    "key": case.key,
                    "original": case.original,
                    "divergent": case.divergent,
                    "status": case.status.value,
                    "resolution": case.resolution,
                }
                for case in self._reviews.values()
            ],
            "disturbances": [
                {
                    "event_id": event.event_id,
                    "parcel_id": event.parcel_id,
                    "kind": event.kind,
                    "occurred_on": event.occurred_on.isoformat(),
                    "severity": event.severity,
                }
                for event in self._disturbances.values()
            ],
            "recoveries": [
                {
                    "action_id": action.action_id,
                    "parcel_id": action.parcel_id,
                    "disturbance_id": action.disturbance_id,
                    "mode": action.mode,
                    "failure_reason": action.failure_reason,
                    "planned_on": action.planned_on.isoformat(),
                    "status": action.status.value,
                }
                for action in self._recoveries.values()
            ],
            "milestones": [
                {
                    "milestone_id": milestone.milestone_id,
                    "commitment_id": milestone.commitment_id,
                    "stage": milestone.stage,
                    "amount": milestone.amount,
                    "currency": milestone.currency,
                    "requirements": [
                        {
                            "indicator": requirement.indicator,
                            "min_confirmed": requirement.min_confirmed,
                        }
                        for requirement in milestone.requirements
                    ],
                    "status": milestone.status.value,
                    "approvals": list(milestone.approvals),
                    "released_on": (
                        milestone.released_on.isoformat()
                        if milestone.released_on
                        else None
                    ),
                    "released_by": milestone.released_by,
                }
                for milestone in self._milestones.values()
            ],
        }

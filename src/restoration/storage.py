"""SQLite 存储层：建表脚本、连接与基础存取。

业务判定在 ``service`` 等模块中完成；本模块只负责持久化结构。
所有时间以 ISO-8601 文本保存，金额以整数最小货币单位保存。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 岗位账户：提交、确认、付款按岗位与具体人员双重区分
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN
                ('secretariat','community_liaison','implementer',
                 'monitor','finance','auditor')),
    organization TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 申报项目（不同出资方/实施机构的恢复承诺各自为一个项目）
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 地块：边界一旦登记，后续补植/重做通过批次与事件留痕，不删改边界
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id    TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id),
    external_ref TEXT NOT NULL,
    parcel_name  TEXT NOT NULL,
    geometry     TEXT NOT NULL,            -- GeoJSON Polygon/MultiPolygon
    area_m2      REAL NOT NULL CHECK (area_m2 > 0),
    land_use     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'registered'
                 CHECK (status IN ('registered','active','rework','withdrawn')),
    created_at   TEXT NOT NULL,
    UNIQUE (project_id, external_ref)
);

-- 权利人/社区权属；敏感行只对授权岗位开放
CREATE TABLE IF NOT EXISTS rights_holders (
    holder_id   TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    holder_name TEXT NOT NULL,
    rights_type TEXT NOT NULL CHECK (rights_type IN
                ('customary','communal','leasehold','state','private','joint')),
    tenure_doc_ref TEXT NOT NULL DEFAULT '',
    sensitive   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- 退化基线
CREATE TABLE IF NOT EXISTS baselines (
    parcel_id        TEXT PRIMARY KEY REFERENCES parcels(parcel_id),
    measured_on      TEXT NOT NULL,
    canopy_cover_pct REAL NOT NULL CHECK (canopy_cover_pct BETWEEN 0 AND 100),
    soil_index       REAL NOT NULL,
    degradation_class TEXT NOT NULL CHECK (degradation_class IN
                     ('light','moderate','severe','very_severe')),
    evidence_ref     TEXT NOT NULL,
    recorded_by      TEXT NOT NULL REFERENCES users(user_id)
);

-- 恢复方案
CREATE TABLE IF NOT EXISTS restoration_plans (
    parcel_id     TEXT PRIMARY KEY REFERENCES parcels(parcel_id),
    approach      TEXT NOT NULL CHECK (approach IN
                  ('natural_regeneration','enrichment_planting',
                   'agroforestry','mangrove_restoration','assisted_natural')),
    target_canopy_pct REAL NOT NULL CHECK (target_canopy_pct BETWEEN 0 AND 100),
    species_mix   TEXT NOT NULL,        -- JSON 数组
    monitor_seasons TEXT NOT NULL,      -- JSON 数组，适用监测季节
    plan_window_years INTEGER NOT NULL CHECK (plan_window_years > 0),
    created_at    TEXT NOT NULL
);

-- 社区协商与申诉
CREATE TABLE IF NOT EXISTS consultations (
    consultation_id TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    held_on     TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('consent','conditional','objection')),
    minutes_ref TEXT NOT NULL,
    sensitive_notes TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grievances (
    grievance_id TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    raised_by_name TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','negotiating','resolved','withdrawn')),
    raised_on   TEXT NOT NULL,
    due_on      TEXT NOT NULL,
    resolution  TEXT NOT NULL DEFAULT ''
);

-- 作业批次：失败与重做均保留，不允许删除
CREATE TABLE IF NOT EXISTS work_batches (
    batch_id    TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    sequence_no INTEGER NOT NULL,
    season      TEXT NOT NULL,
    planted_area_m2 REAL NOT NULL CHECK (planted_area_m2 > 0),
    species_mix TEXT NOT NULL,
    inputs_cost_minor INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL CHECK (status IN
                ('planned','established','failed','rework','verified')),
    failure_reason TEXT NOT NULL DEFAULT '',
    predecessor_batch_id TEXT REFERENCES work_batches(batch_id),
    created_by  TEXT NOT NULL REFERENCES users(user_id),
    created_at  TEXT NOT NULL,
    UNIQUE (parcel_id, sequence_no)
);

-- 火灾/病虫害等灾害事件
CREATE TABLE IF NOT EXISTS disaster_events (
    event_id    TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    batch_id    TEXT REFERENCES work_batches(batch_id),
    kind        TEXT NOT NULL CHECK (kind IN
                ('fire','pest','disease','drought','flood','windthrow')),
    occurred_on TEXT NOT NULL,
    area_affected_m2 REAL NOT NULL CHECK (area_affected_m2 >= 0),
    severity    TEXT NOT NULL CHECK (severity IN ('minor','moderate','severe')),
    cause_note  TEXT NOT NULL DEFAULT '',
    response    TEXT NOT NULL DEFAULT 'pending'
                CHECK (response IN ('pending','replant','redo','accepted_loss')),
    response_batch_id TEXT REFERENCES work_batches(batch_id),
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    created_at  TEXT NOT NULL
);

-- 监测样方
CREATE TABLE IF NOT EXISTS plots (
    plot_id     TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL REFERENCES parcels(parcel_id),
    plot_code   TEXT NOT NULL,
    location    TEXT NOT NULL,           -- GeoJSON Point
    protocol_id TEXT,                    -- 首批观测后冻结
    established_season TEXT NOT NULL,
    UNIQUE (parcel_id, plot_code)
);

-- 采集方法协议：同一 method_key 的新版本不能改动已冻结的样方时间序列
CREATE TABLE IF NOT EXISTS protocols (
    protocol_id     TEXT PRIMARY KEY,
    method_key      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    metric          TEXT NOT NULL,
    unit            TEXT NOT NULL,
    valid_min       REAL,
    valid_max       REAL,
    delta_warn      REAL,                -- 相对基线的异文阈值（按单位）
    applicable_seasons TEXT NOT NULL,    -- JSON 数组
    definition_ref  TEXT NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (method_key, version)
);

-- 原始观测：幂等键唯一，异文数据进入复核而不参与确认
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    plot_id   TEXT NOT NULL REFERENCES plots(plot_id),
    protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id),
    season    TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    value     REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    collected_by TEXT NOT NULL REFERENCES users(user_id),
    status    TEXT NOT NULL CHECK (status IN ('accepted','pending_review','rejected')),
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT REFERENCES users(user_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_plot_season
    ON observations(plot_id, season);

-- 估算结果（碳与生计指标）：确认后冻结，后续模型不能改写
CREATE TABLE IF NOT EXISTS estimates (
    estimate_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    metric   TEXT NOT NULL,
    unit     TEXT NOT NULL,
    season   TEXT NOT NULL,
    model_version TEXT NOT NULL,
    value    REAL NOT NULL,
    basis_observation_ids TEXT NOT NULL,  -- JSON 数组
    status   TEXT NOT NULL CHECK (status IN ('draft','confirmed')),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    confirmed_by TEXT REFERENCES users(user_id),
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (parcel_id, metric, season, model_version)
);

-- 资金来源
CREATE TABLE IF NOT EXISTS funding_sources (
    source_id TEXT PRIMARY KEY,
    funder_name TEXT NOT NULL,
    currency TEXT NOT NULL,
    total_committed_minor INTEGER NOT NULL CHECK (total_committed_minor >= 0)
);

CREATE TABLE IF NOT EXISTS funding_allocations (
    source_id TEXT NOT NULL REFERENCES funding_sources(source_id),
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    PRIMARY KEY (source_id, parcel_id)
);

-- 付款里程碑：证据、提交人、批准人、付款人全程留痕
CREATE TABLE IF NOT EXISTS payment_milestones (
    milestone_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    source_id TEXT NOT NULL REFERENCES funding_sources(source_id),
    stage    TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    status   TEXT NOT NULL CHECK (status IN
             ('pending','evidence_submitted','approved','paid',
              'suspended','withheld')),
    evidence_estimate_ids TEXT NOT NULL DEFAULT '[]',
    evidence_batch_id TEXT REFERENCES work_batches(batch_id),
    submitted_by TEXT REFERENCES users(user_id),
    submitted_at TEXT,
    approved_by TEXT REFERENCES users(user_id),
    approved_at TEXT,
    paid_by TEXT REFERENCES users(user_id),
    paid_at TEXT,
    hold_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 社区收益（生计指标的落点）
CREATE TABLE IF NOT EXISTS community_benefits (
    benefit_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    milestone_id TEXT REFERENCES payment_milestones(milestone_id),
    benefit_type TEXT NOT NULL CHECK (benefit_type IN
                 ('cash_transfer','jobs','infrastructure','training',
                  'tenure_support','in_kind')),
    description TEXT NOT NULL,
    amount_minor INTEGER NOT NULL DEFAULT 0,
    recipient_count INTEGER NOT NULL DEFAULT 0,
    recorded_on TEXT NOT NULL
);

-- 持久任务队列：重启后继续到期监测、待协商申诉、暂停资金
CREATE TABLE IF NOT EXISTS task_queue (
    task_id   TEXT PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN
              ('due_monitoring','grievance_consultation','payment_suspension')),
    ref_key   TEXT NOT NULL,             -- 业务去重键，同周期任务不重复
    payload   TEXT NOT NULL,             -- JSON
    status    TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','in_progress','done','failed')),
    run_after TEXT NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    last_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, ref_key)
);

-- 只增的审计日志
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor_id TEXT,
    actor_role TEXT,
    action   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    result   TEXT NOT NULL CHECK (result IN ('allowed','denied')),
    detail   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """SQLite 连接封装。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._apply_schema()

    def _apply_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def commit(self) -> None:
        self.conn.commit()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(text: str) -> Any:
    return json.loads(text)

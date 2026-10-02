"""持久任务队列：到期监测、待协商申诉、暂停资金。

任务全部落在 SQLite 中，处理进程重启后会：

1. 把崩溃时停留在 in_progress 的任务复位为 pending；
2. 继续领取 run_after 已到期的 pending 任务。

任务按 (kind, ref_key) 去重，重复入队不会产生两条任务；未完成的
周期性任务（等观测、等申诉结论）会推后 run_after 而不是被标记完成。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Callable

from .auth import Actor, AccessControl, now_iso
from .storage import Database, dumps, loads

DUE_MONITORING = "due_monitoring"
GRIEVANCE_CONSULTATION = "grievance_consultation"
PAYMENT_SUSPENSION = "payment_suspension"

# 处理器返回 (finished, note)：finished=False 表示条件未满足，稍后重试
Handler = Callable[[dict], "tuple[bool, str]"]


def _new_id() -> str:
    return f"tsk_{uuid.uuid4().hex[:12]}"


class TaskQueue:
    def __init__(self, db: Database, ac: AccessControl) -> None:
        self.db = db
        self.ac = ac
        self._handlers: dict[str, Handler] = {}

    def register_handler(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def recover(self) -> int:
        """重启恢复：把未完成的 in_progress 任务复位。返回复位数。"""
        cur = self.db.execute(
            "UPDATE task_queue SET status='pending', updated_at=?"
            " WHERE status='in_progress'",
            (now_iso(),),
        )
        self.db.commit()
        return cur.rowcount

    def enqueue(
        self,
        kind: str,
        ref_key: str,
        payload: dict,
        *,
        run_after: str | None = None,
    ) -> str:
        """入队（幂等）。已存在的同键任务不重复创建。"""
        ts = now_iso()
        run_after = run_after or ts
        existing = self.db.query_one(
            "SELECT task_id, status FROM task_queue WHERE kind=? AND ref_key=?",
            (kind, ref_key),
        )
        if existing is not None:
            # 同键任务已终结（申诉重开、里程碑再次暂停）时重新激活；
            # 仍在队列中的任务保持幂等，不覆盖其调度
            if existing["status"] in ("done", "failed"):
                self.db.execute(
                    "UPDATE task_queue SET status='pending', payload=?, run_after=?,"
                    " last_note='', updated_at=? WHERE task_id=?",
                    (dumps(payload), run_after, ts, existing["task_id"]),
                )
                self.db.commit()
            return existing["task_id"]
        task_id = _new_id()
        self.db.execute(
            "INSERT INTO task_queue(task_id, kind, ref_key, payload, status,"
            " run_after, attempts, created_at, updated_at)"
            " VALUES (?,?,?,?, 'pending', ?,0,?,?)",
            (task_id, kind, ref_key, dumps(payload), run_after or ts, ts, ts),
        )
        self.db.commit()
        return task_id

    def run_due(self, actor: Actor, *, now: str | None = None, limit: int = 20) -> list[dict]:
        """领取并处理到期任务，返回每个任务的处理结果。"""
        self.ac.require(actor, "task.run", ("task_queue", ""))
        moment = now or now_iso()
        rows = self.db.query_all(
            "SELECT * FROM task_queue WHERE status='pending' AND run_after<=?"
            " ORDER BY run_after LIMIT ?",
            (moment, limit),
        )
        results: list[dict] = []
        for row in rows:
            result = self._process(row, moment)
            results.append(result)
        return results

    def _process(self, row, moment: str) -> dict:
        task_id = row["task_id"]
        kind = row["kind"]
        handler = self._handlers.get(kind)
        self.db.execute(
            "UPDATE task_queue SET status='in_progress', attempts=attempts+1,"
            " updated_at=? WHERE task_id=?",
            (moment, task_id),
        )
        self.db.commit()

        if handler is None:
            self._finish(task_id, "failed", "缺少任务处理器", moment, delay_minutes=None)
            return {"task_id": task_id, "kind": kind, "outcome": "failed", "note": "缺少任务处理器"}

        payload = loads(row["payload"])
        try:
            finished, note = handler(payload)
        except Exception as exc:  # 处理器异常不丢任务，稍后重试
            self._finish(task_id, "pending", f"处理异常: {exc}", moment, delay_minutes=60)
            return {"task_id": task_id, "kind": kind, "outcome": "retry", "note": str(exc)}

        if finished:
            self._finish(task_id, "done", note, moment, delay_minutes=None)
            outcome = "done"
        else:
            self._finish(task_id, "pending", note, moment, delay_minutes=60)
            outcome = "waiting"
        return {"task_id": task_id, "kind": kind, "outcome": outcome, "note": note}

    def _finish(
        self, task_id: str, status: str, note: str, moment: str, *, delay_minutes: int | None
    ) -> None:
        run_after = moment
        if delay_minutes is not None:
            base = _parse(moment) + timedelta(minutes=delay_minutes)
            run_after = base.isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE task_queue SET status=?, last_note=?, run_after=?, updated_at=?"
            " WHERE task_id=?",
            (status, note, run_after, moment, task_id),
        )
        self.db.commit()

    def list_tasks(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM task_queue WHERE status=? ORDER BY run_after", (status,)
            )
        else:
            rows = self.db.query_all("SELECT * FROM task_queue ORDER BY run_after")
        return [dict(r) for r in rows]

    def pending_count(self) -> int:
        return int(
            self.db.query_one("SELECT COUNT(*) AS n FROM task_queue WHERE status='pending'")["n"]
        )


def _parse(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)

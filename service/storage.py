"""只追加事件存储。

存储以 JSONL 文件持久化（受控环境可替换为数据库）。每次追加后 ``fsync``；
服务启动（含崩溃恢复）时重放全部事件，内存投影完全由事件重建。若最后一行
因写入中途崩溃而损坏，恢复时将其截断，保证日志始终可重放。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import EVENT_TYPES, Event


class EventStore:
    """线程安全的只追加事件日志。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._path: Path | None = Path(path) if path else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._replay()

    # ---------- 读取 ----------

    def _replay(self) -> None:
        """启动/恢复时从文件重放事件，丢弃崩溃残留的半行。"""

        assert self._path is not None
        if not self._path.exists():
            return
        offset = 0
        with self._path.open("rb") as fh:
            for raw in fh:
                end = offset + len(raw)
                line = raw.decode("utf-8").strip()
                if not line:
                    offset = end
                    continue
                try:
                    event = Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
                    # 末行写了一半：截断到上一条完整事件之后
                    with self._path.open("r+b") as repair:
                        repair.truncate(offset)
                    return
                self._events.append(event)
                offset = end
        if self._events:
            seqs = [e.seq for e in self._events]
            if seqs != list(range(1, len(self._events) + 1)):
                raise RuntimeError("事件日志序号不连续，存储可能已损坏")

    def all_events(self, scenario: str | None = None) -> list[Event]:
        """返回事件的只读快照；``scenario=None`` 仅基线事件。"""

        with self._lock:
            return [e for e in self._events if e.scenario == scenario]

    def all_events_including_scenarios(self) -> list[Event]:
        """返回全部事件（基线 + 所有情景叠加）。"""

        with self._lock:
            return list(self._events)

    def events_for_scenario(self, scenario: str | None) -> list[Event]:
        """基线事件，或基线 + 指定情景的叠加事件。"""

        with self._lock:
            if scenario is None:
                return [e for e in self._events if e.scenario is None]
            return [e for e in self._events if e.scenario is None or e.scenario == scenario]

    @property
    def next_seq(self) -> int:
        with self._lock:
            return len(self._events) + 1

    @property
    def last_recorded_at(self) -> str | None:
        with self._lock:
            return self._events[-1].recorded_at if self._events else None

    # ---------- 写入 ----------

    def append(
        self,
        event_type: str,
        payload: dict,
        *,
        occurred_on: str,
        source: str,
        source_batch: str,
        scenario: str | None = None,
        event_id: str | None = None,
        recorded_at: str | None = None,
    ) -> Event:
        """校验并追加一个事件（落盘后才对内存生效）。"""

        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{event_type}")
        with self._lock:
            event = Event(
                seq=len(self._events) + 1,
                event_id=event_id or f"evt-{uuid.uuid4().hex[:12]}",
                event_type=event_type,
                occurred_on=occurred_on,
                recorded_at=recorded_at
                or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                source=source,
                source_batch=source_batch,
                scenario=scenario,
                payload=payload,
            )
            if self._path is not None:
                line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
            self._events.append(event)
            return event

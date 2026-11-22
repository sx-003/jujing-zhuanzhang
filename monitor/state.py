"""跨轮状态：去重、源健康、运行记录、按天归档。

状态文件提交在仓库的 state 分支（不是 Actions cache）：缓存被逐出会导致每轮重发。
推送失败不写 seen，下轮重试；干跑写入模拟推送状态便于验证。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

UTC = timezone.utc
RETENTION_DAYS = 7
ARCHIVE_DIR = "state/archive"
# 测试与离线自检可以把归档写到临时目录，避免样例数据污染 state 分支的真实归档。
def archive_dir() -> str:
    return os.environ.get("STATE_ARCHIVE_DIR") or ARCHIVE_DIR


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    raw = (url or "").strip().split("#", 1)[0]
    raw = re.sub(r"^https?://(www\.)?", "", raw, flags=re.I)
    if "?" in raw:
        path, query = raw.split("?", 1)
        kept = [p for p in query.split("&") if p and not p.lower().startswith(("utm_", "ref=", "spm="))]
        raw = path + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/")


class State:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: Dict[str, dict] = {}
        self.archived: Dict[str, dict] = {}
        self.source_failures: Dict[str, dict] = {}
        self.daily_queries: Dict[str, Dict[str, str]] = {}
        self.runs: List[dict] = []
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "State":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = dict(raw.get("seen") or {})
        self.archived = dict(raw.get("archived") or {})
        self.source_failures = dict(raw.get("source_failures") or {})
        self.daily_queries = dict(raw.get("daily_queries") or {})
        self.runs = list(raw.get("runs") or [])[-288:]
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "schema_version": "1.0.0",
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "seen": self.seen,
            "archived": self.archived,
            "source_failures": self.source_failures,
            "daily_queries": self.daily_queries,
            "runs": self.runs[-288:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_run(self, started_at: str) -> float:
        prior = _parse(self.last_run)
        current = _parse(started_at)
        gap = round((current - prior).total_seconds() / 60, 1) if prior and current else -1
        self.runs.append({"at": started_at, "gap_minutes": gap})
        self.last_run = started_at
        return gap

    def add_source_status(self, name: str, ok: bool, error_type: str = "", error: str = "") -> Optional[str]:
        """返回健康告警文本（连续 3 轮失败时），恢复后清除。"""
        entry = dict(self.source_failures.get(name) or {})
        if ok:
            if entry.get("count", 0) >= 3:
                self.source_failures[name] = {"count": 0, "recovered_at": _now(), "last_error": ""}
                return f"来源恢复：{name}"
            self.source_failures.pop(name, None)
            return None
        count = int(entry.get("count") or 0) + 1
        self.source_failures[name] = {
            "count": count, "error_type": error_type, "last_error": error[:300], "at": _now()
        }
        if count == 3:
            return f"来源连续 3 轮失败：{name}（{error_type}）"
        return None

    def is_used_today(self, query: str, today: str) -> bool:
        return query in self.daily_queries.get(today, {})

    def mark_queries(self, queries: List[str], today: str) -> None:
        bucket = self.daily_queries.setdefault(today, {})
        for query in queries:
            bucket[query] = _now()

    def already_seen(self, key: str) -> bool:
        return key in self.seen

    def already_archived(self, key: str) -> bool:
        return key in self.archived

    def mark_archived(self, key: str, record: dict) -> None:
        entry = dict(record)
        entry["first_seen"] = entry.get("first_seen") or _now()
        self.archived[key] = entry

    def mark_pushed(self, key: str, record: dict) -> None:
        entry = dict(record)
        entry["first_seen"] = entry.get("first_seen") or _now()
        entry["pushed_at"] = _now()
        self.seen[key] = entry

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        removed = 0
        for key, meta in list(self.seen.items()):
            stamp = _parse(str(meta.get("first_seen") or ""))
            if stamp is not None and stamp < cutoff:
                self.seen.pop(key, None)
                removed += 1
        for key, meta in list(self.archived.items()):
            stamp = _parse(str(meta.get("first_seen") or ""))
            if stamp is not None and stamp < cutoff:
                self.archived.pop(key, None)
        today = datetime.now(UTC).date()
        for day in list(self.daily_queries):
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                parsed = None
            if parsed is None or (today - parsed).days > RETENTION_DAYS:
                self.daily_queries.pop(day, None)
        return removed

    def archive(self, records: List[dict], day: Optional[str] = None,
                base_dir: str = "") -> str:
        """按天归档留档文件；铸销与借贷质押已在过滤阶段排除，不会进来。"""
        if not records:
            return ""
        stamp = day or datetime.now(UTC).strftime("%Y-%m-%d")
        target = Path(base_dir or archive_dir()) / f"{stamp}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return str(target)

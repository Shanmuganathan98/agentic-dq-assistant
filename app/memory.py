"""Memory of human decisions: the agent proposes the resolution a reviewer chose last time."""
from datetime import datetime, timezone
from app import db


def record(resolutions: dict, decided_by: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for check, resolution in resolutions.items():
        db.execute(
            "INSERT INTO memory_decisions (check_name, resolution, decided_by, decided_at, times_used) "
            "VALUES (?, ?, ?, ?, 1) ON CONFLICT (check_name) DO UPDATE SET resolution = excluded.resolution, "
            "decided_by = excluded.decided_by, decided_at = excluded.decided_at, "
            "times_used = memory_decisions.times_used + 1", (check, resolution, decided_by, now))


def all_decisions() -> list[dict]:
    db.init_schema()
    return db.query("SELECT check_name, resolution, decided_by, decided_at, times_used "
                    "FROM memory_decisions ORDER BY check_name")


def suggestions() -> dict:
    return {r["check_name"]: r["resolution"] for r in all_decisions()}

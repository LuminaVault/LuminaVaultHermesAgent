"""Read-only session views across every profile on this machine.

The gateway API server serves one profile's ``state.db``. A VPS running
several profiles (``default``, ``mac-mcp``, …) behind one API key needs to
report all of them, so a remote dashboard can list every agent's sessions
without a second process per profile.

Each profile's ``state.db`` is opened read-only: these calls run on dashboard
refreshes and must never DDL or write-lock another profile's live database.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# A session with no end and activity inside this window counts as live.
ACTIVE_WINDOW_SECONDS = 300


class UnknownProfileError(LookupError):
    """The requested profile name is invalid or does not exist."""


def profile_targets(profile: Optional[str] = None) -> List[Tuple[str, Path]]:
    """``(name, home)`` for one named profile, or for every profile."""
    from hermes_cli import profiles as profiles_mod

    if profile and profile != "all":
        try:
            name = profiles_mod.normalize_profile_name(profile)
            profiles_mod.validate_profile_name(name)
        except ValueError as exc:
            raise UnknownProfileError(str(exc)) from exc
        if not profiles_mod.profile_exists(name):
            raise UnknownProfileError(f"Profile '{name}' does not exist.")
        return [(name, Path(profiles_mod.get_profile_dir(name)))]

    try:
        targets = [(info.name, Path(info.path)) for info in profiles_mod.list_profiles()]
    except Exception:
        targets = []
    return targets or [("default", Path(profiles_mod.get_profile_dir("default")))]


def profile_names() -> List[str]:
    return [name for name, _ in profile_targets()]


def _open_read_only(home: Path):
    from hermes_state import SessionDB

    db_path = home / "state.db"
    if not db_path.exists():
        return None
    return SessionDB(db_path=db_path, read_only=True)


def is_active(session: Dict[str, Any], now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    last = session.get("last_active") or session.get("started_at") or 0
    return session.get("ended_at") is None and (now - last) < ACTIVE_WINDOW_SECONDS


def list_sessions(
    *,
    limit: int,
    offset: int = 0,
    source: Optional[str] = None,
    profile: Optional[str] = None,
    project: Callable[[Dict[str, Any]], Dict[str, Any]] = dict,
) -> Dict[str, Any]:
    """Sessions from every profile (or one), newest activity first.

    Each row is ``project(row)`` tagged with ``profile`` and ``is_active``.
    A profile whose database cannot be read is reported in ``errors``
    instead of failing the whole list.
    """
    # Over-fetch per profile so the merged window is right for this page.
    per_profile = min(limit + offset, 500)
    merged: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    now = time.time()
    for name, home in profile_targets(profile):
        try:
            db = _open_read_only(home)
        except Exception as exc:
            errors.append({"profile": name, "error": str(exc)})
            continue
        if db is None:
            continue
        try:
            rows = db.list_sessions_rich(
                source=source,
                limit=per_profile,
                offset=0,
                include_children=False,
                order_by_last_active=True,
            )
            for row in rows:
                out = project(row)
                out["profile"] = name
                out["is_active"] = is_active(row, now)
                merged.append(out)
        except Exception as exc:
            errors.append({"profile": name, "error": str(exc)})
        finally:
            db.close()

    merged.sort(key=lambda s: s.get("last_active") or s.get("started_at") or 0, reverse=True)
    window = merged[offset:offset + limit]
    return {
        "data": window,
        "has_more": len(merged) > offset + limit,
        "errors": errors,
    }


def session_messages(profile: str, session_id: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """``(resolved_session_id, messages)`` for a session in ``profile``.

    ``None`` when that profile has no such session.
    """
    [(_, home)] = profile_targets(profile)
    db = _open_read_only(home)
    if db is None:
        return None
    try:
        if not db.get_session(session_id):
            return None
        resolved = db.resolve_resume_session_id(session_id)
        return resolved, db.get_messages(resolved)
    finally:
        db.close()

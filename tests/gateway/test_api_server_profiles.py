"""Cross-profile read-only routes on the API server.

``/api/instance``, ``/api/profiles/sessions`` and
``/api/profiles/{profile}/sessions/{id}/messages`` let a remote dashboard
list every profile's sessions on this machine through one API key.
"""

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli import profile_sessions
from hermes_state import SessionDB


def _make_app(api_key: str = "sk-secret") -> web.Application:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": api_key}))
    app = web.Application()
    app.router.add_get("/api/instance", adapter._handle_instance)
    app.router.add_get("/api/profiles/sessions", adapter._handle_profiles_sessions)
    app.router.add_get(
        "/api/profiles/{profile}/sessions/{session_id}/messages",
        adapter._handle_profile_session_messages,
    )
    return app


AUTH = {"Authorization": "Bearer sk-secret"}


def _seed(home: Path, sessions):
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    try:
        for session_id, source, messages in sessions:
            db.create_session(session_id, source)
            for role, content in messages:
                db.append_message(session_id, role, content)
    finally:
        db.close()


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    homes = {"default": tmp_path / "default", "mac-mcp": tmp_path / "mac-mcp"}
    _seed(homes["default"], [("cron_1", "cron", [("assistant", "daily brief")])])
    _seed(homes["mac-mcp"], [("tg_1", "telegram", [("user", "hi"), ("assistant", "hello")])])

    def targets(profile=None):
        if profile and profile != "all":
            if profile not in homes:
                raise profile_sessions.UnknownProfileError(f"Profile '{profile}' does not exist.")
            return [(profile, homes[profile])]
        return list(homes.items())

    monkeypatch.setattr(profile_sessions, "profile_targets", targets)
    return homes


@pytest.mark.asyncio
async def test_routes_require_the_api_key(two_profiles):
    async with TestClient(TestServer(_make_app())) as cli:
        for path in ("/api/instance", "/api/profiles/sessions", "/api/profiles/default/sessions/cron_1/messages"):
            resp = await cli.get(path)
            assert resp.status == 401, path


@pytest.mark.asyncio
async def test_instance_lists_every_profile(two_profiles):
    async with TestClient(TestServer(_make_app())) as cli:
        resp = await cli.get("/api/instance", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        assert body["object"] == "hermes.instance"
        assert body["profiles"] == ["default", "mac-mcp"]
        assert body["version"]


@pytest.mark.asyncio
async def test_sessions_from_all_profiles_are_tagged(two_profiles):
    async with TestClient(TestServer(_make_app())) as cli:
        resp = await cli.get("/api/profiles/sessions", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        by_id = {row["id"]: row for row in body["data"]}
        assert set(by_id) == {"cron_1", "tg_1"}
        assert by_id["tg_1"]["profile"] == "mac-mcp"
        assert by_id["tg_1"]["source"] == "telegram"
        assert by_id["cron_1"]["profile"] == "default"
        assert all(isinstance(row["is_active"], bool) for row in body["data"])
        # Client-safe projection: no system prompt leaks through.
        assert all("system_prompt" not in row for row in body["data"])
        assert body["errors"] == []


@pytest.mark.asyncio
async def test_sessions_filter_by_profile_and_source(two_profiles):
    async with TestClient(TestServer(_make_app())) as cli:
        resp = await cli.get("/api/profiles/sessions?profile=mac-mcp", headers=AUTH)
        assert [row["id"] for row in (await resp.json())["data"]] == ["tg_1"]

        resp = await cli.get("/api/profiles/sessions?source=cron", headers=AUTH)
        assert [row["id"] for row in (await resp.json())["data"]] == ["cron_1"]

        resp = await cli.get("/api/profiles/sessions?profile=nope", headers=AUTH)
        assert resp.status == 404


@pytest.mark.asyncio
async def test_messages_come_from_the_named_profile(two_profiles):
    async with TestClient(TestServer(_make_app())) as cli:
        resp = await cli.get("/api/profiles/mac-mcp/sessions/tg_1/messages", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        assert body["profile"] == "mac-mcp"
        assert [(m["role"], m["content"]) for m in body["data"]] == [("user", "hi"), ("assistant", "hello")]

        # The session exists, but in another profile.
        resp = await cli.get("/api/profiles/default/sessions/tg_1/messages", headers=AUTH)
        assert resp.status == 404

        resp = await cli.get("/api/profiles/nope/sessions/tg_1/messages", headers=AUTH)
        assert resp.status == 404

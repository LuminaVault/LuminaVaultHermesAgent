"""Terminal output on the /v1/runs stream (LuminaVault addition).

See ``gateway/platforms/api_run_terminal.py``. The behaviours pinned here are
the ones a consumer that persists every run event depends on: output is
routed to the run that spawned it and nowhere else, it is coalesced rather
than one event per line, a runaway process cannot stream without bound, and
nothing leaves unredacted.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import api_run_terminal as rt
from tests.gateway.test_api_server_runs import _create_runs_app, _make_adapter

SECRET = "sk-" + "a" * 40


class ManualTimer:
    """Stands in for threading.Timer so a test fires the flush itself."""

    instances: list = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.cancelled = False
        self.daemon = False
        ManualTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _proc(pid="proc_1", run="run_a"):
    return SimpleNamespace(id=pid, session_key=run)


@pytest.fixture
def router():
    ManualTimer.instances = []
    clock = Clock()
    r = rt.RunTerminalRouter(clock=clock, timer_factory=ManualTimer)
    r.clock = clock
    return r


# --- tool.started / tool.completed fields ---------------------------------


class TestFields:
    def test_started_carries_the_command(self):
        assert rt.started_fields("terminal", {"command": "ls -la", "background": True}) == {
            "command": "ls -la",
            "background": True,
        }

    def test_other_tools_add_nothing(self):
        assert rt.started_fields("read_file", {"path": "x"}) == {}
        assert rt.completed_fields("read_file", '{"output": "x"}') == {}

    def test_completed_reads_the_terminal_json(self):
        fields = rt.completed_fields("terminal", json.dumps({"output": "a\nb", "exit_code": 2}))
        assert fields == {"output": "a\nb", "output_truncated": False, "exit_code": 2}

    def test_a_plain_text_result_is_output_with_no_exit_code(self):
        fields = rt.completed_fields("terminal", "blocked by policy")
        assert fields["output"] == "blocked by policy"
        assert "exit_code" not in fields

    def test_a_boolean_is_not_an_exit_code(self):
        fields = rt.completed_fields("terminal", json.dumps({"output": "", "exit_code": True}))
        assert "exit_code" not in fields

    def test_long_output_keeps_the_tail(self):
        text = "x" * rt.MAX_OUTPUT_CHARS + "THE ERROR"
        fields = rt.completed_fields("terminal", json.dumps({"output": text, "exit_code": 1}))
        assert fields["output_truncated"] is True
        assert fields["output"].endswith("THE ERROR")
        assert len(fields["output"]) == rt.MAX_OUTPUT_CHARS

    def test_secrets_are_redacted_in_command_and_output(self):
        started = rt.started_fields("terminal", {"command": f"export OPENAI_API_KEY={SECRET}"})
        completed = rt.completed_fields("terminal", json.dumps({"output": f"key={SECRET}", "exit_code": 0}))
        assert SECRET not in started["command"]
        assert SECRET not in completed["output"]


# --- background output routing -----------------------------------------------


class TestRouter:
    def test_output_reaches_only_its_own_run(self, router):
        a, b = [], []
        router.register("run_a", a.append)
        router.register("run_b", b.append)
        router.on_output(_proc(run="run_a"), "x" * rt.FLUSH_CHARS)

        assert [e["chunk"] for e in a] == ["x" * rt.FLUSH_CHARS]
        assert b == []

    def test_output_from_an_unknown_run_is_dropped(self, router):
        seen = []
        router.register("run_a", seen.append)
        router.on_output(_proc(run="someone_else"), "hello")
        router.unregister("run_a")
        assert seen == []

    def test_small_chunks_are_coalesced_until_the_timer(self, router):
        seen = []
        router.register("run_a", seen.append)
        for line in ["one\n", "two\n", "three\n"]:
            router.on_output(_proc(), line)
        assert seen == []
        assert len(ManualTimer.instances) == 1

        ManualTimer.instances[0].fire()
        assert [e["chunk"] for e in seen] == ["one\ntwo\nthree\n"]
        assert seen[0]["event"] == "terminal.output"
        assert seen[0]["process_id"] == "proc_1"

    def test_a_chunk_after_the_interval_flushes_at_once(self, router):
        seen = []
        router.register("run_a", seen.append)
        router.on_output(_proc(), "first\n")
        router.clock.now += rt.FLUSH_INTERVAL
        router.on_output(_proc(), "second\n")
        assert [e["chunk"] for e in seen] == ["first\nsecond\n"]

    def test_processes_keep_separate_buffers(self, router):
        seen = []
        router.register("run_a", seen.append)
        router.on_output(_proc("p1"), "from one")
        router.on_output(_proc("p2"), "from two")
        router.unregister("run_a")
        assert {(e["process_id"], e["chunk"]) for e in seen} == {("p1", "from one"), ("p2", "from two")}

    def test_unregister_flushes_what_is_buffered(self, router):
        seen = []
        router.register("run_a", seen.append)
        router.on_output(_proc(), "tail")
        router.unregister("run_a")
        assert [e["chunk"] for e in seen] == ["tail"]
        router.on_output(_proc(), "after the run")
        assert len(seen) == 1

    def test_a_runaway_process_stops_at_the_budget_with_one_marker(self, router, monkeypatch):
        monkeypatch.setattr(rt, "MAX_STREAM_CHARS", 10_000)
        seen = []
        router.register("run_a", seen.append)
        for _ in range(20):
            router.on_output(_proc(), "y" * rt.FLUSH_CHARS)

        streamed = sum(len(e["chunk"]) for e in seen)
        markers = [e for e in seen if e.get("truncated")]
        assert streamed == 10_000
        assert len(markers) == 1

    def test_chunks_are_redacted(self, router):
        seen = []
        router.register("run_a", seen.append)
        router.on_output(_proc(), f"token={SECRET}\n" + "z" * rt.FLUSH_CHARS)
        assert seen and SECRET not in seen[0]["chunk"]

    def test_install_chains_an_existing_sink_and_is_idempotent(self, router):
        calls = []
        registry = SimpleNamespace(on_output=lambda session, chunk: calls.append(chunk))
        router.install(registry)
        router.install(registry)
        registry.on_output(_proc(), "hi")
        assert calls == ["hi"]

    def test_a_failing_consumer_never_breaks_the_reader_thread(self, router):
        def explode(_event):
            raise RuntimeError("consumer went away")

        router.register("run_a", explode)
        router.on_output(_proc(), "q" * rt.FLUSH_CHARS)  # must not raise


# --- end to end through /v1/runs --------------------------------------------


class TestRunStream:
    @pytest.mark.asyncio
    async def test_the_run_stream_carries_command_output_and_background_chunks(self):
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        from tools.approval import get_current_session_key

        seen_key = {}

        def run_conversation(user_message=None, conversation_history=None, task_id=None):
            cb = create.call_args.kwargs["tool_progress_callback"]
            cb("tool.started", "terminal", "ls", {"command": "ls"})
            cb(
                "tool.completed", "terminal", None, None,
                duration=0.2, is_error=False,
                result=json.dumps({"output": "a.txt\nb.txt", "exit_code": 0}),
            )
            # A background process the run started writes output. Its
            # session_key is the run's bound session key.
            seen_key["key"] = get_current_session_key(default="")
            proc = SimpleNamespace(id="proc_9", session_key=seen_key["key"])
            adapter._run_terminal_router.on_output(proc, "server listening\n")
            return {"final_response": "done"}

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as create:
                agent = MagicMock()
                agent.run_conversation.side_effect = run_conversation
                agent.session_prompt_tokens = 0
                agent.session_completion_tokens = 0
                agent.session_total_tokens = 0
                create.return_value = agent

                resp = await cli.post("/v1/runs", json={"input": "list files"})
                run_id = (await resp.json())["run_id"]
                body = await (await cli.get(f"/v1/runs/{run_id}/events")).text()

        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        by_type = {e["event"]: e for e in events}

        assert seen_key["key"] == run_id
        assert by_type["tool.started"]["command"] == "ls"
        assert by_type["tool.completed"]["output"] == "a.txt\nb.txt"
        assert by_type["tool.completed"]["exit_code"] == 0
        terminal = [e for e in events if e["event"] == "terminal.output"]
        assert [(e["process_id"], e["chunk"]) for e in terminal] == [("proc_9", "server listening\n")]
        assert all(e["run_id"] == run_id for e in terminal)
        # The run's own events still end the stream the way they always did.
        assert events[-1]["event"] == "run.completed"

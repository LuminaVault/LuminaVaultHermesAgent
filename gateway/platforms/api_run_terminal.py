"""Terminal output on the ``/v1/runs`` event stream (LuminaVault addition).

Upstream's desktop app shows an agent's terminal live, but only over the
``tui_gateway`` WebSocket: ``process_registry.on_output`` is wired by
``tui_gateway`` alone, and ``/v1/runs`` drops the terminal tool's command and
result on the floor. A client that drives Hermes through ``/v1/runs`` -- which
is every LuminaVault client -- therefore had no way to see what the agent ran
or what it printed.

This module adds three things to the run stream, and nothing else:

* ``tool.started`` for the terminal tool carries ``command`` and
  ``background``.
* ``tool.completed`` for the terminal tool carries the tail of ``output``,
  ``exit_code`` and ``output_truncated``.
* A background process spawned by a run streams ``terminal.output`` events
  ``{process_id, chunk}`` to that run, coalesced.

Coalescing is not an optimisation. Consumers persist every run event -- the
LuminaVault server writes one Postgres row per event -- and a process reader
thread can hand over a chunk per line. So chunks are buffered per process and
flushed at most every ``FLUSH_INTERVAL`` seconds or ``FLUSH_CHARS`` characters,
and a run stops streaming after ``MAX_STREAM_CHARS`` with one
``truncated: true`` marker, rather than turning ``yes`` into a million rows.

Everything that leaves here goes through ``redact_sensitive_text(force=True)``,
as ``subagent.*`` free text already does: terminal output is the most likely
place for a token to appear. Redaction runs on coalesced chunks, so a secret is
only missed if it straddles a flush boundary.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from agent.redact import redact_sensitive_text

#: Tools whose command and output are surfaced. Only the shell: other tools'
#: results are structured data, not a terminal.
TERMINAL_TOOLS = frozenset({"terminal"})

#: Tail kept from a finished command's output. The head of a long build is
#: rarely what anyone needs; the error at the end is.
MAX_OUTPUT_CHARS = 16_000

#: Background output a single run may stream before it stops.
MAX_STREAM_CHARS = 256_000

FLUSH_INTERVAL = 0.25
FLUSH_CHARS = 4_096

Push = Callable[[Dict[str, Any]], None]


def _redact(text: str) -> str:
    return redact_sensitive_text(text, force=True)


def started_fields(tool_name: Optional[str], args: Any) -> Dict[str, Any]:
    """Extra ``tool.started`` fields for a terminal call, else ``{}``."""
    if tool_name not in TERMINAL_TOOLS or not isinstance(args, dict):
        return {}
    command = args.get("command")
    if not isinstance(command, str) or not command:
        return {}
    return {"command": _redact(command), "background": bool(args.get("background"))}


def completed_fields(tool_name: Optional[str], result: Any) -> Dict[str, Any]:
    """Extra ``tool.completed`` fields for a terminal call, else ``{}``.

    The terminal tool returns a JSON string with ``output`` and ``exit_code``.
    Anything else -- a plain-text error, a blocked call -- is passed through
    as output with no exit code rather than guessed at.
    """
    if tool_name not in TERMINAL_TOOLS or result is None:
        return {}
    output: Any = result
    exit_code: Optional[int] = None
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            output = parsed.get("output", "")
            code = parsed.get("exit_code")
            exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    truncated = len(text) > MAX_OUTPUT_CHARS
    if truncated:
        text = text[-MAX_OUTPUT_CHARS:]
    fields: Dict[str, Any] = {"output": _redact(text), "output_truncated": truncated}
    if exit_code is not None:
        fields["exit_code"] = exit_code
    return fields


class _Route:
    """One run's background output: per-process buffers and a budget."""

    def __init__(self, push: Push, clock: Callable[[], float]):
        self.push = push
        self.clock = clock
        self.buffers: Dict[str, str] = {}
        self.first_buffered_at: Dict[str, float] = {}
        self.streamed = 0
        self.exhausted = False
        self.timer: Optional[threading.Timer] = None


class RunTerminalRouter:
    """Routes ``process_registry`` output to the run that spawned the process.

    Keyed by the process's ``session_key``. ``/v1/runs`` binds each run's
    approval session key to its ``run_id`` and the terminal tool stamps a
    spawned process with the current session key, so the key is the run.
    ``task_id`` would not do: the terminal tool collapses it to the shared
    container key (``"default"``), which every run has.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ):
        self._routes: Dict[str, _Route] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._timer_factory = timer_factory
        self._previous_sink: Optional[Callable[[Any, str], None]] = None

    def install(self, registry: Any) -> None:
        """Hook the registry's live-output sink, chaining any sink already set
        so a co-hosted driver (the desktop gateway) keeps working."""
        existing = getattr(registry, "on_output", None)
        if existing == self.on_output:
            return
        self._previous_sink = existing
        registry.on_output = self.on_output

    def register(self, run_key: str, push: Push) -> None:
        if not run_key:
            return
        with self._lock:
            self._routes[run_key] = _Route(push, self._clock)

    def unregister(self, run_key: str) -> None:
        """Flushes what is buffered, then stops routing. Output a process
        writes after its run has ended has nowhere to go and is dropped."""
        with self._lock:
            route = self._routes.pop(run_key, None)
            if route is None:
                return
            self._cancel_timer(route)
            events = self._drain(route)
        for event in events:
            self._safe_push(route, event)

    def on_output(self, session: Any, chunk: str) -> None:
        previous = self._previous_sink
        if previous is not None:
            try:
                previous(session, chunk)
            except Exception:
                pass
        if not chunk:
            return
        run_key = str(getattr(session, "session_key", "") or "")
        process_id = str(getattr(session, "id", "") or "")
        if not run_key or not process_id:
            return
        events = []
        with self._lock:
            route = self._routes.get(run_key)
            if route is None or route.exhausted:
                return
            if process_id not in route.buffers:
                route.buffers[process_id] = ""
                route.first_buffered_at[process_id] = route.clock()
            route.buffers[process_id] += chunk
            size = len(route.buffers[process_id])
            age = route.clock() - route.first_buffered_at[process_id]
            if size >= FLUSH_CHARS or age >= FLUSH_INTERVAL:
                events = self._drain(route)
                self._cancel_timer(route)
            elif route.timer is None:
                route.timer = self._timer_factory(FLUSH_INTERVAL, lambda: self._flush_key(run_key))
                try:
                    route.timer.daemon = True
                except Exception:
                    pass
                route.timer.start()
        for event in events:
            self._safe_push(route, event)

    def _flush_key(self, run_key: str) -> None:
        with self._lock:
            route = self._routes.get(run_key)
            if route is None:
                return
            route.timer = None
            events = self._drain(route)
        for event in events:
            self._safe_push(route, event)

    def _drain(self, route: _Route) -> list:
        """Buffered chunks as events, within the run's budget. Caller holds
        the lock; pushing happens outside it."""
        events = []
        for process_id, text in list(route.buffers.items()):
            if route.exhausted:
                break
            if not text:
                continue
            remaining = MAX_STREAM_CHARS - route.streamed
            if remaining <= 0 and not route.exhausted:
                route.exhausted = True
                events.append({"process_id": process_id, "chunk": "", "truncated": True})
                break
            if len(text) > remaining:
                text = text[:remaining]
            route.streamed += len(text)
            events.append({"process_id": process_id, "chunk": _redact(text)})
            if route.streamed >= MAX_STREAM_CHARS and not route.exhausted:
                route.exhausted = True
                events.append({"process_id": process_id, "chunk": "", "truncated": True})
        route.buffers.clear()
        route.first_buffered_at.clear()
        return events

    @staticmethod
    def _cancel_timer(route: _Route) -> None:
        if route.timer is not None:
            try:
                route.timer.cancel()
            except Exception:
                pass
            route.timer = None

    @staticmethod
    def _safe_push(route: _Route, fields: Dict[str, Any]) -> None:
        try:
            route.push({"event": "terminal.output", "timestamp": time.time(), **fields})
        except Exception:
            pass

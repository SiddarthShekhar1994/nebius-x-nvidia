"""Keeper's hosted demo: a small web server for a Hugging Face Docker Space (space/README.md).

Standard library only. Each visitor gets a private session (a temporary Keeper database and
router log, see engine.py). The page is public, so model calls are capped per visitor and per
day, and the Token Factory key stays in the server's environment.

    TOKENFACTORY_API_KEY=... python -m space.server   # then open http://localhost:7860
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from keeper_core.adapters import AuthenticationError
from keeper_core.time import to_storage

from . import engine

STATIC = Path(__file__).resolve().parent / "static"
PAGES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.css": ("app.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
SECURITY = {
    "Content-Security-Policy": "default-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'self' https://huggingface.co",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
SESSION_ID = re.compile(r"[0-9a-f]{32}")
MAX_REQUEST = 16_384


class DemoError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Job:
    session: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    events: list = field(default_factory=list)
    done: bool = False
    error: str | None = None
    state: dict | None = None
    created: float = field(default_factory=time.monotonic)


class Demo:
    """Sessions and background jobs; one step at a time per visitor."""

    def __init__(self, root: Path, models, budget: engine.Budget, *, live: bool, model_names: dict,
                 per_session: int = 40, idle_s: float = 2 * 3600, max_sessions: int = 300, workers: int = 3):
        self.root, self.models, self.budget, self.live = Path(root), models, budget, live
        self.model_names, self.per_session = model_names, per_session
        self.idle_s, self.max_sessions = idle_s, max_sessions
        self.sessions: dict[str, engine.Session] = {}
        self.seen: dict[str, float] = {}
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._workers = threading.BoundedSemaphore(workers)

    def config(self) -> dict:
        return {"live": self.live, "clock": to_storage(engine.CLOCK), "models": self.model_names,
                "per_session": self.per_session, "per_day": self.budget.per_day,
                "used_today": self.budget.used()}

    def session(self, sid: str) -> engine.Session:
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            session = self.sessions.get(sid)
            if session is None:
                if len(self.sessions) >= self.max_sessions:
                    raise DemoError(HTTPStatus.SERVICE_UNAVAILABLE, "The demo is busy. Try again in a few minutes.")
                session = self.sessions[sid] = engine.Session(self.root / sid)
            self.seen[sid] = now
            return session

    def _expire(self, now: float) -> None:
        for sid, seen in list(self.seen.items()):
            session = self.sessions[sid]
            if now - seen > self.idle_s and not session.lock.locked():
                del self.sessions[sid], self.seen[sid]
                shutil.rmtree(session.directory, ignore_errors=True)
        for job_id, job in list(self.jobs.items()):
            if job.done and now - job.created > 3600:
                del self.jobs[job_id]

    def state(self, sid: str) -> dict:
        session = self.session(sid)
        if not session.lock.acquire(blocking=False):
            return {"busy": True, "job": session.job}
        try:
            return engine.state(session)
        finally:
            session.lock.release()

    def start(self, sid: str, action, fields: dict) -> str:
        if action not in engine.CALLS:
            raise DemoError(HTTPStatus.BAD_REQUEST, "Unknown step.")
        if engine.CALLS[action] and not self.live:
            raise DemoError(HTTPStatus.SERVICE_UNAVAILABLE,
                            "The demo isn't connected to Token Factory right now, so Keeper can't read mail.")
        session = self.session(sid)
        if not session.lock.acquire(blocking=False):
            raise DemoError(HTTPStatus.CONFLICT, "Keeper is still working on your last step.")
        try:
            engine.check(session, action)
            email = (engine.written_email(session, fields.get("sender"), fields.get("subject"), fields.get("body"))
                     if action == "email" else None)
            calls = engine.CALLS[action]
            if session.calls + calls > self.per_session:
                raise DemoError(HTTPStatus.TOO_MANY_REQUESTS,
                                "This visit has used its model calls. Start over to begin a new one.")
            if not self.budget.reserve(calls):
                raise DemoError(HTTPStatus.TOO_MANY_REQUESTS,
                                "The demo has used today's model calls. They reset at midnight UTC.")
            session.calls += calls
            job = Job(sid)
            with self._lock:
                self.jobs[job.id] = job
            session.job = job.id
            threading.Thread(target=self._run, args=(job, session, action, email), daemon=True).start()
            return job.id
        except engine.StepError as exc:
            session.lock.release()
            raise DemoError(HTTPStatus.CONFLICT, str(exc)) from None
        except BaseException:
            session.lock.release()
            raise

    def _run(self, job: Job, session: engine.Session, action: str, email) -> None:
        try:
            with self._workers:
                engine.run_step(session, action, self.models, job.events.append, email=email)
        except AuthenticationError:
            job.error = "Token Factory rejected the demo's key, so Keeper can't reach Nemotron right now."
        except engine.StepError as exc:
            job.error = str(exc)
        except Exception:
            traceback.print_exc()
            job.error = "Something went wrong on the server. Try again, or start over."
        finally:
            try:
                job.state = engine.state(session)
            except Exception:
                traceback.print_exc()
            finally:
                session.lock.release()
                job.done = True

    def job(self, job_id: str, since: int) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise DemoError(HTTPStatus.NOT_FOUND, "That step has expired. Reload the page.")
        done = job.done  # read before the events, so a finished job never drops its last events
        events = job.events[since:]
        return {"events": events, "next": since + len(events), "done": done,
                "error": job.error if done else None, "state": job.state if done else None}

    def reset(self, sid: str) -> None:
        with self._lock:
            session = self.sessions.get(sid)
            if session is None:
                return
            if not session.lock.acquire(blocking=False):
                raise DemoError(HTTPStatus.CONFLICT, "Keeper is still working. Start over when it finishes.")
            del self.sessions[sid]
            self.seen.pop(sid, None)
        shutil.rmtree(session.directory, ignore_errors=True)


def _sid(value) -> str:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise DemoError(HTTPStatus.BAD_REQUEST, "Bad session. Reload the page.")
    return value


def handler_for(demo: Demo, *, log: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KeeperDemo"
        sys_version = ""

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in PAGES:
                    name, kind = PAGES[url.path]
                    return self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), kind)
                if url.path == "/healthz":
                    return self._send(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")
                if url.path == "/api/config":
                    return self._json(demo.config())
                if url.path == "/api/state":
                    return self._json(demo.state(_sid(query.get("session", [""])[0])))
                if url.path == "/api/job":
                    since = query.get("since", ["0"])[0]
                    if not since.isdigit():
                        raise DemoError(HTTPStatus.BAD_REQUEST, "Bad request.")
                    return self._json(demo.job(query.get("id", [""])[0], int(since)))
                raise DemoError(HTTPStatus.NOT_FOUND, "Not found.")
            except DemoError as exc:
                self._json({"error": exc.message}, exc.status)

        def do_POST(self):
            try:
                body = self._body()
                sid = _sid(body.get("session"))
                path = urlparse(self.path).path
                if path == "/api/run":
                    fields = body.get("email") if isinstance(body.get("email"), dict) else {}
                    return self._json({"job": demo.start(sid, body.get("action"), fields)}, HTTPStatus.ACCEPTED)
                if path == "/api/reset":
                    demo.reset(sid)
                    return self._json({"ok": True})
                raise DemoError(HTTPStatus.NOT_FOUND, "Not found.")
            except DemoError as exc:
                self._json({"error": exc.message}, exc.status)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise DemoError(HTTPStatus.LENGTH_REQUIRED, "Bad request.") from None
            if not 0 < length <= MAX_REQUEST:
                raise DemoError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "That request is too large.")
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                raise DemoError(HTTPStatus.BAD_REQUEST, "Bad request.") from None
            if not isinstance(data, dict):
                raise DemoError(HTTPStatus.BAD_REQUEST, "Bad request.")
            return data

        def _json(self, data, status=HTTPStatus.OK):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _send(self, status, payload: bytes, kind: str):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in SECURITY.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_request(self, code="-", size="-"):
            # Paths only: query strings carry session IDs.
            if not log:
                return
            status = code.value if isinstance(code, HTTPStatus) else code
            sys.stderr.write(f"{self.command} {urlparse(self.path).path} {status}\n")

    return Handler


def main() -> None:
    from router.config import load_settings
    settings = load_settings()
    root = Path(tempfile.mkdtemp(prefix="keeper-demo-"))
    demo = Demo(root, engine.live_models, engine.Budget(int(os.environ.get("KEEPER_DEMO_DAILY_CALLS", "600"))),
                live=bool(settings.api_key),
                model_names={"triage": settings.models["nano"], "drafts": settings.models["super"]})
    port = int(os.environ.get("PORT", "7860"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(demo))
    server.daemon_threads = True
    print(f"Keeper demo on port {port}; Token Factory {'connected' if demo.live else 'not configured'}",
          file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

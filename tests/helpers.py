"""A fake transport for the tests, the Python twin of the JavaScript SDK's fakeFetch."""

from __future__ import annotations

import copy
import email.message
import io
import json
import pathlib
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from socials_homes.cli import CliIo, main

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    """A fresh copy of a fixture, safe to change in a test."""
    return copy.deepcopy(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


@dataclass
class Call:
    request: urllib.request.Request
    timeout: Optional[float]

    @property
    def url(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.request.full_url)

    @property
    def href(self) -> str:
        return self.request.full_url

    @property
    def path(self) -> str:
        return self.url.path

    @property
    def search(self) -> str:
        """The query with its "?", or "" without one, as URL.search gives it."""
        return f"?{self.url.query}" if self.url.query else ""

    def param(self, name: str) -> Optional[str]:
        values = urllib.parse.parse_qs(self.url.query, keep_blank_values=True).get(name)
        return values[0] if values else None


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: email.message.Message) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


Step = dict[str, Any]
Route = Union[Step, Callable[[Call], Step], list[Any]]


@dataclass
class FakeOpener:
    """Answers from a route table and records every request.

    A route is a step {status, body, headers, raw}, a function (call) -> step,
    or a list of those answered in turn (the last one repeats). An error status
    is raised as urllib.error.HTTPError, as urllib's own opener does; with
    raise_errors=False it is returned as a response instead.
    """

    routes: dict[str, Route] = field(default_factory=dict)
    raise_errors: bool = True
    calls: list[Call] = field(default_factory=list)
    turns: dict[str, int] = field(default_factory=dict)

    def open(self, fullurl: urllib.request.Request, data: Any = None, timeout: Optional[float] = None) -> FakeResponse:
        call = Call(fullurl, timeout)
        self.calls.append(call)
        key = f"{call.path}{call.search}"
        route = self.routes.get(key, self.routes.get(call.path))
        if route is None:
            return self._respond(call, {"status": 404, "body": fixture("problem-city-not-found")})
        steps = route if isinstance(route, list) else [route]
        n = self.turns.get(key, 0)
        self.turns[key] = n + 1
        step = steps[min(n, len(steps) - 1)]
        return self._respond(call, step(call) if callable(step) else step)

    def _respond(self, call: Call, step: Step) -> FakeResponse:
        status = step.get("status", 200)
        headers = email.message.Message()
        headers["Content-Type"] = "application/problem+json" if status >= 400 else "application/json"
        for k, v in step.get("headers", {}).items():
            headers[k] = v
        raw = step.get("raw")
        body = (
            raw.encode("utf-8")
            if isinstance(raw, str)
            else raw
            if raw is not None
            else json.dumps(step.get("body")).encode()
        )
        if status >= 400 and self.raise_errors:
            raise urllib.error.HTTPError(call.href, status, "error", headers, io.BytesIO(body))
        return FakeResponse(status, body, headers)


class FailingOpener:
    """Raises the same exception on every request and counts the attempts."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def open(self, fullurl: urllib.request.Request, data: Any = None, timeout: Optional[float] = None) -> Any:
        self.calls += 1
        raise self.error


class HangingOpener:
    """Never answers within its timeout, then raises socket.timeout, as a socket does."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, fullurl: urllib.request.Request, data: Any = None, timeout: Optional[float] = None) -> Any:
        self.calls += 1
        threading.Event().wait(timeout)
        raise socket.timeout("timed out")


class DeafOpener:
    """Ignores its timeout and holds the call for five seconds."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, fullurl: urllib.request.Request, data: Any = None, timeout: Optional[float] = None) -> Any:
        self.calls += 1
        threading.Event().wait(5)
        raise AssertionError("the client should have given up long ago")


@dataclass
class CliResult:
    code: int
    out: str
    err: str
    opener: Any


def api() -> FakeOpener:
    """The routes the CLI tests share."""

    def cities(call: Call) -> Step:
        if call.param("country") == "XX":
            return {"body": {"data": [], "next_cursor": None}}
        return {"body": {**fixture("cities-nl-page1"), "next_cursor": None}}

    return FakeOpener(
        {
            "/api/v1": {"body": fixture("index")},
            "/api/v1/plans": {"body": fixture("plans")},
            "/api/v1/cities": cities,
            "/api/v1/cities/amsterdam": {"body": fixture("city-amsterdam")},
            "/api/v1/cities/amsterdam/supply": {"body": fixture("supply-amsterdam-900")},
            "/api/v1/cities/den_haag": {"status": 404, "body": fixture("problem-city-not-found")},
        }
    )


def cli(args: list[str], opener: Any = None) -> CliResult:
    opener = api() if opener is None else opener
    out: list[str] = []
    err: list[str] = []
    code = main(args, CliIo(stdout=out.append, stderr=err.append, opener=opener))
    return CliResult(code, "\n".join(out), "\n".join(err), opener)


class LocalApi:
    """A real HTTP server on 127.0.0.1, for what only real sockets show: redirects, slow bodies, threads.

        with LocalApi() as base_url: ...

    /api/v1 answers {"ok": true}. /moved/... answers 308 to the same path without /moved,
    /ftp/... answers 302 to an ftp URL, /slow/... sends a byte every 0.1 seconds for 30 seconds,
    and /short/... promises 100 bytes, sends 12 and hangs up. hits lists every path asked for.
    """

    def __init__(self) -> None:
        self.hits: list[str] = []

    def __enter__(self) -> str:
        import http.server
        import socketserver
        import time

        hits = self.hits

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                hits.append(self.path)
                if self.path.startswith("/short/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                    self.close_connection = True
                elif self.path.startswith("/moved/"):
                    self.send_response(308)
                    self.send_header("Location", self.path[len("/moved") :])
                    self.end_headers()
                elif self.path.startswith("/ftp/"):
                    self.send_response(302)
                    self.send_header("Location", "ftp://127.0.0.1/file")
                    self.end_headers()
                elif self.path.startswith("/slow/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "300")
                    self.end_headers()
                    try:
                        for _ in range(300):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        pass  # the client hung up, as it should
                else:
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def request_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "socials-homes-request")

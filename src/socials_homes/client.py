"""A client for the Socials Homes API, on the standard library alone."""

from __future__ import annotations

import contextlib
import email.utils
import http.client
import ipaddress
import json
import math
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import timezone
from typing import IO, TYPE_CHECKING, Any, Literal, NamedTuple, Optional, Protocol, Union, cast

from ._version import VERSION
from .types import ApiIndex, City, CityList, CitySupply, PlansResponse, Problem

if TYPE_CHECKING:
    from http.client import HTTPMessage

DEFAULT_BASE_URL = "https://www.socials.homes"

# The longest wait the JavaScript SDK allows (its setTimeout ceiling), kept here
# so both clients accept the same options. In seconds.
_MAX_SECONDS = 2_147_483.647
_MAX_RETRIES = 2_147_483_647

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

ClientErrorCode = Literal["timeout", "connection_error", "invalid_response"]
"""Codes the client adds for failures that never reached an API answer."""


class Opener(Protocol):
    """The transport the client sends through. urllib.request.OpenerDirector fits.

    open() gets a GET urllib.request.Request and the timeout in seconds. It
    returns a response with status, headers and read(), or raises
    urllib.error.HTTPError for an error status, as urllib's own opener does.
    """

    def open(self, fullurl: urllib.request.Request, *, timeout: float) -> Any: ...


class SocialsHomesError(Exception):
    """What the client raises for a failed API call.

    For an API error, status, code and problem are the API's own RFC 9457
    answer. For a failure before an answer (timeout, network, a body that is
    not the API's JSON) status is the HTTP status when there was one, else 0,
    and problem is None. The original exception, if any, is __cause__.

    Bad arguments (an empty slug, a malformed base_url) are not a
    SocialsHomesError: they raise ValueError or TypeError before any request.
    """

    message: str
    status: int
    code: str
    """The API's code (see types.ProblemCode) or one of ClientErrorCode."""
    url: str
    problem: Optional[Problem]
    retry_after_seconds: Optional[int]
    """Seconds the API asked to wait, on a 429."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str,
        url: str,
        problem: Optional[Problem] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.url = url
        self.problem = problem
        self.retry_after_seconds = retry_after_seconds

    @property
    def resolution(self) -> Optional[str]:
        """The API's hint on how to fix the request, when it gave one."""
        if self.problem is None:
            return None
        hint = self.problem.get("resolution")
        return hint if isinstance(hint, str) else None

    def __reduce__(self) -> tuple[Any, ...]:
        # Keyword-only arguments need help to survive pickling (multiprocessing).
        return (
            _rebuild_error,
            (type(self), self.message, self.status, self.code, self.url, self.problem, self.retry_after_seconds),
        )


def _rebuild_error(
    cls: type[SocialsHomesError],
    message: str,
    status: int,
    code: str,
    url: str,
    problem: Optional[Problem],
    retry_after_seconds: Optional[int],
) -> SocialsHomesError:
    return cls(message, status=status, code=code, url=url, problem=problem, retry_after_seconds=retry_after_seconds)


class _Answer(NamedTuple):
    status: int
    body: dict[str, Any]


class _Failure(NamedTuple):
    error: SocialsHomesError
    # "no", "backoff", or the seconds a 429 asked to wait.
    retry: Union[Literal["no", "backoff"], float]


class _Exchange(NamedTuple):
    status: int
    retry_after: Optional[str]
    raw: bytes


class _TimedOut(Exception):
    """The attempt's deadline passed before the opener answered."""


class SocialsHomes:
    """A client for the Socials Homes API.

    Every endpoint is public and read-only: no API key, no account, nothing to
    configure. The client holds no connection or state, so one instance can be
    shared across threads.

        sh = SocialsHomes()
        supply = sh.get_city_supply("amsterdam", budget_max_eur=900)["supply"]
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        max_retry_delay: float = 30.0,
        retry_base_delay: float = 0.5,
        user_agent: Optional[str] = None,
        opener: Optional[Opener] = None,
    ) -> None:
        """Build a client. Every argument is checked here, before any request.

        base_url: where the API lives, an http or https origin with an optional
            path. Default https://www.socials.homes.
        timeout: give up on one attempt after this many seconds. Default 15.
        max_retries: extra attempts after a 429, a 5xx or a network failure.
            Default 2. Every call is a GET, so retrying is safe.
        max_retry_delay: the longest Retry-After the client waits out, in
            seconds. A longer one is raised as rate_limited at once. Default 30.
        retry_base_delay: first backoff after a 5xx or a network failure,
            doubled each time, in seconds. Default 0.5.
        user_agent: added to the User-Agent, for example "my-agent/1.2".
        opener: the transport, for tests or a proxy setup. Default a urllib
            opener that follows redirects to http and https only.
        """
        self._base_url = _parse_base_url(DEFAULT_BASE_URL if base_url is None else base_url)
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise TypeError("opener must have an open(request, timeout=...) method, like an OpenerDirector.")
        self._opener: Opener = opener if opener is not None else _default_opener()
        self._timeout = _seconds_option(timeout, "timeout", 0.001)
        self._max_retries = _count_option(max_retries, "max_retries")
        self._max_retry_delay = _seconds_option(max_retry_delay, "max_retry_delay", 0)
        self._retry_base_delay = _seconds_option(retry_base_delay, "retry_base_delay", 0)
        self._user_agent = _user_agent(user_agent)

    @property
    def base_url(self) -> str:
        """The API origin and path, without a trailing slash."""
        return self._base_url

    def get_index(self) -> ApiIndex:
        """GET /api/v1: every endpoint, the spec, the docs, the limits and the MCP servers."""
        return cast(ApiIndex, self._get("", {}))

    def list_cities(
        self,
        *,
        country: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> CityList:
        """GET /api/v1/cities: one page of covered cities, ordered by slug.

        country: an ISO 3166-1 alpha-2 code ("NL") or an English country name
            ("Netherlands"). An empty string is sent: the API answers why it is wrong.
        limit: cities per page, 1 to 200. Default 50.
        cursor: next_cursor from the previous page.
        """
        _check_str(country, "country")
        _check_int(limit, "limit")
        _check_str(cursor, "cursor")
        return cast(CityList, self._get("/cities", {"country": country, "limit": limit, "cursor": cursor}))

    def cities(self, *, country: Optional[str] = None, page_size: int = 200) -> Iterator[City]:
        """Every covered city, fetching the pages as it goes.

        page_size: cities fetched per request, 1 to 200. Default 200.
        """
        _check_str(country, "country")
        _check_int(page_size, "page_size")
        return self._walk_cities(country, page_size)

    def get_city(self, slug: str) -> City:
        """GET /api/v1/cities/{slug}: one covered city. An unknown slug raises city_not_found with suggestions."""
        return cast(City, self._get(f"/cities/{_encode_slug(slug)}", {}, unwrap=True))

    def get_city_supply(self, slug: str, *, budget_max_eur: Optional[int] = None) -> CitySupply:
        """GET /api/v1/cities/{slug}/supply: live rental listings in the city now.

        budget_max_eur: also count the listings at or under this monthly rent, a
            whole number of EUR from 50 to 20000.

        When counts cannot be read, supply["available"] is False with a reason.
        That is not zero.
        """
        path = f"/cities/{_encode_slug(slug)}/supply"
        _check_int(budget_max_eur, "budget_max_eur")
        return cast(CitySupply, self._get(path, {"budget_max_eur": budget_max_eur}, unwrap=True))

    def list_plans(self) -> PlansResponse:
        """GET /api/v1/plans: every plan with its EUR price, renewal, inclusions and the VIP guarantee."""
        return cast(PlansResponse, self._get("/plans", {}))

    def _walk_cities(self, country: Optional[str], page_size: int) -> Iterator[City]:
        href = f"{self._base_url}/api/v1/cities"
        seen: set[str] = set()
        cursor: Optional[str] = None
        while True:
            page = self.list_cities(country=country, limit=page_size, cursor=cursor)
            data = page.get("data")
            if not isinstance(data, list):
                raise _invalid_response(href, 200, "a page without a data list")
            yield from data
            cursor = page.get("next_cursor")
            if cursor is None:
                return
            if not isinstance(cursor, str):
                raise _invalid_response(href, 200, "a next_cursor that is not a string")
            if cursor in seen:
                raise _invalid_response(href, 200, "the same next_cursor twice")
            seen.add(cursor)

    def _get(self, path: str, query: Mapping[str, Union[str, int, None]], *, unwrap: bool = False) -> dict[str, Any]:
        href = f"{self._base_url}/api/v1{path}"
        # An empty string is sent as it is: the API answers it with a 400 that says why.
        pairs = [f"{_form_quote(k)}={_form_quote(str(v))}" for k, v in query.items() if v is not None]
        if pairs:
            href = f"{href}?{'&'.join(pairs)}"
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}

        attempt = 0
        while True:
            outcome = self._attempt(href, headers)
            if isinstance(outcome, _Answer):
                if not unwrap:
                    return outcome.body
                data = outcome.body.get("data")
                if not isinstance(data, dict):
                    raise _invalid_response(href, outcome.status, "a body without a data object")
                return data
            if attempt >= self._max_retries or outcome.retry == "no":
                raise outcome.error
            _sleep(self._backoff(attempt) if outcome.retry == "backoff" else outcome.retry)
            attempt += 1

    def _attempt(self, href: str, headers: Mapping[str, str]) -> Union[_Answer, _Failure]:
        # base_url was checked to be http or https when the client was built.
        request = urllib.request.Request(href, headers=dict(headers), method="GET")  # noqa: S310
        try:
            exchange = self._send(request)
        except Exception as cause:  # noqa: BLE001 - any opener failure is a connection_error, as with fetch
            timed_out = isinstance(cause, _TimedOut) or _is_timeout(cause)
            if timed_out:
                message = f"No answer from {href} within {_format_seconds(self._timeout)}."
                error = SocialsHomesError(message, status=0, code="timeout", url=href)
            else:
                error = SocialsHomesError(f"Could not reach {href}.", status=0, code="connection_error", url=href)
            if not isinstance(cause, _TimedOut):
                error.__cause__ = cause
            return _Failure(error, "backoff")

        status = exchange.status
        body = _parse_json(exchange.raw)
        if 200 <= status < 300:
            if body is _NOT_JSON:
                return _Failure(_invalid_response(href, status, "a body that is not JSON"), "no")
            if not isinstance(body, dict):
                return _Failure(_invalid_response(href, status, "JSON that is not an object"), "no")
            return _Answer(status, body)

        problem = cast(Problem, body) if _is_problem(body) else None
        retry_after_seconds = _retry_after(exchange.retry_after, problem)
        if problem is not None:
            error = SocialsHomesError(
                problem["detail"],
                status=status,
                code=problem["code"],
                url=href,
                problem=problem,
                retry_after_seconds=retry_after_seconds,
            )
        else:
            error = _invalid_response(href, status, f"HTTP {status} without a problem body")
        if status not in _RETRY_STATUSES:
            return _Failure(error, "no")
        if status == 429:
            wait = 1 if retry_after_seconds is None else retry_after_seconds
            # A long wait is the caller's decision, not a silent hang.
            return _Failure(error, "no" if wait > self._max_retry_delay else float(wait))
        return _Failure(error, "backoff")

    def _send(self, request: urllib.request.Request) -> _Exchange:
        """One exchange, given up at the deadline even when the opener ignores its timeout.

        The socket timeout bounds each blocking step, not the whole exchange, so
        a server sending a byte now and then could hold a call for ever. The
        exchange runs in a daemon thread and the caller waits for it at most
        timeout seconds. An abandoned exchange stops at its next read and closes
        its response; a server gone silent is cut off by the socket timeout.
        """
        abandoned = threading.Event()
        done = threading.Event()
        result: list[_Exchange] = []
        failure: list[BaseException] = []

        def work() -> None:
            try:
                result.append(self._exchange(request, abandoned))
            except BaseException as err:  # noqa: BLE001 - re-raised in the caller's thread
                failure.append(err)
            finally:
                done.set()

        try:
            threading.Thread(target=work, name="socials-homes-request", daemon=True).start()
        except RuntimeError:
            # No thread to be had (a subinterpreter allows no daemon threads):
            # run here, bounded by the socket timeout alone.
            return self._exchange(request, abandoned)
        try:
            finished = done.wait(self._timeout)
        finally:
            # At the deadline, or on Ctrl-C during the wait, the worker stops at its next read.
            if not done.is_set():
                abandoned.set()
        if not finished:
            raise _TimedOut
        if failure:
            raise failure[0]
        return result[0]

    def _exchange(self, request: urllib.request.Request, abandoned: threading.Event) -> _Exchange:
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as err:
            # urllib raises for a status it does not follow: the error is the answer.
            try:
                # An HTTPError made without a body has fp None, and on Python 3.9 no file to read or close.
                body = _read(err, abandoned) if _attribute(err, "fp") is not None else b""
                return _Exchange(int(err.code), _header(_attribute(err, "headers"), "Retry-After"), body)
            finally:
                _close(err)
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            return _Exchange(int(status), _header(response.headers, "Retry-After"), _read(response, abandoned))
        finally:
            _close(response)

    def _backoff(self, attempt: int) -> float:
        base = self._retry_base_delay * 2 ** min(attempt, 30)
        return float(min(base + random.random() * base * 0.25, self._max_retry_delay))  # noqa: S311 - jitter, not crypto


# The one place the client waits, so tests can watch the delays.
_sleep = time.sleep


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects like a browser's fetch, but only to http and https."""

    # Python 3.9 and 3.10 know no 308. Every call is a GET, so it is followed as a 307 is.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            # Not followed: the 3xx itself becomes the answer, an invalid_response.
            return None
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)


def _default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HttpOnlyRedirects)


def _read(response: Any, abandoned: threading.Event) -> bytes:
    """The whole body, a read at a time, so an abandoned exchange stops at its next read."""
    read1 = _attribute(response, "read1")
    if not callable(read1):
        return bytes(response.read())
    chunks: list[bytes] = []
    while not abandoned.is_set():
        chunk = bytes(read1(65536))
        if chunk:
            chunks.append(chunk)
            continue
        body = b"".join(chunks)
        # The connection closed before the Content-Length was reached: a cut answer, as read() reports it.
        left = _attribute(response, "length")
        if isinstance(left, int) and not isinstance(left, bool) and left > 0:
            raise http.client.IncompleteRead(body, left)
        return body
    raise _TimedOut


def _close(response: Any) -> None:
    close = _attribute(response, "close")
    if callable(close):
        # The answer is read already; a close that fails changes nothing.
        with contextlib.suppress(Exception):
            close()


def _attribute(obj: Any, name: str) -> Any:
    # An HTTPError built without a body raises KeyError for unknown attributes on Python 3.9.
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - an object that cannot say has no such attribute
        return None


def _form_quote(value: str) -> str:
    """A query value as URLSearchParams writes it: a space is +, * stays, ~ is escaped, a lone surrogate is U+FFFD."""
    text = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return urllib.parse.quote_plus(text, safe="*").replace("~", "%7E")


# What JavaScript's String.prototype.trim removes, so both SDKs read a slug alike.
_JS_SPACE = (
    "\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _is_timeout(err: BaseException) -> bool:
    if isinstance(err, (socket.timeout, TimeoutError)):
        return True
    return isinstance(err, urllib.error.URLError) and isinstance(err.reason, (socket.timeout, TimeoutError))


def _format_seconds(seconds: float) -> str:
    text = format(seconds, ".15g")
    return f"{text} second" if seconds == 1 else f"{text} seconds"


def _seconds_option(value: object, name: str, minimum: float) -> float:
    rule = f"{name} must be a number of seconds from {format(minimum, 'g')} to {_MAX_SECONDS}."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(rule)
    if not math.isfinite(value) or value < minimum or value > _MAX_SECONDS:
        raise ValueError(rule)
    return float(value)


def _count_option(value: object, name: str) -> int:
    rule = f"{name} must be a whole number from 0 to {_MAX_RETRIES}."
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(rule)
    if value < 0 or value > _MAX_RETRIES:
        raise ValueError(rule)
    return value


def _check_str(value: object, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string, got {type(value).__name__}.")


def _check_int(value: object, name: str) -> None:
    # bool is an int to Python, but True is never a page size or a budget.
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} must be a whole number, got {type(value).__name__}.")


_C0_AND_SPACE = "".join(chr(c) for c in range(0x21))
_FORBIDDEN_HOST = re.compile(r"[#%/:<>?@\[\\\]^|]")


def _parse_base_url(raw: object) -> str:
    bad = f'base_url must be an http or https URL, for example {DEFAULT_BASE_URL}. Got "{raw}".'
    if not isinstance(raw, str):
        raise TypeError(bad)
    # A URL parser strips spaces and controls at the ends; inside, they are no URL.
    text = raw.strip(_C0_AND_SPACE)
    if re.search(r"[\x00-\x20\x7f\\]", text):
        raise ValueError(bad)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise ValueError(bad) from None
    scheme = parts.scheme.lower()
    if not scheme:
        raise ValueError(bad)
    if scheme not in ("http", "https"):
        raise ValueError(f"base_url must use http or https, not {scheme}:")
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("base_url must be an origin with an optional path: no query, fragment or credentials.")
    # The host and port read here, not by urlsplit, whose rules vary across Python releases.
    authority = re.fullmatch(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[^:\[\]]+))(?::(?P<port>[0-9]*))?", parts.netloc)
    if authority is None:
        raise ValueError(bad)
    if authority["v6"] is not None:
        try:
            host = f"[{_ipv6(ipaddress.IPv6Address(authority['v6']))}]"
        except ValueError:
            raise ValueError(bad) from None
    else:
        host = authority["name"].lower()
        if _FORBIDDEN_HOST.search(host):
            raise ValueError(bad)
        if not host.isascii():
            # Python's idna codec (IDNA 2003) and a browser's URL parser map some
            # names to different hosts, straße.de for one, so take the xn-- form only.
            raise ValueError(f"base_url must use an ASCII host name: write {host} in its xn-- form.")
        host = _ipv4(host, bad)
    port = int(authority["port"]) if authority["port"] else None
    if port is not None and port > 65535:
        raise ValueError(bad)
    if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    path = _without_dot_segments(urllib.parse.quote(parts.path, safe="/%!$&'()*+,;=:@-._~|^[]"))
    return f"{scheme}://{host}{path}".rstrip("/")


def _ipv4(host: str, bad: str) -> str:
    """The URL standard's IPv4 parser: a host ending in a number is an address, 2130706433 is 127.0.0.1."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if not (re.fullmatch(r"[0-9]+", parts[-1]) or _ipv4_number(parts[-1]) is not None):
        return host
    numbers = [_ipv4_number(part) for part in parts]
    if len(parts) > 4 or None in numbers:
        raise ValueError(bad)
    values = cast(list[int], numbers)
    if any(n > 255 for n in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):
        raise ValueError(bad)
    address = values[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(values[:-1]))
    return ".".join(str((address >> shift) & 255) for shift in (24, 16, 8, 0))


def _ipv4_number(part: str) -> Optional[int]:
    if part == "":
        return None
    radix = 10
    if part[:2] in ("0x", "0X"):
        radix, part = 16, part[2:]
    elif len(part) > 1 and part[0] == "0":
        radix, part = 8, part[1:]
    if part == "":
        return 0
    digits = {10: "0123456789", 16: "0123456789abcdefABCDEF", 8: "01234567"}[radix]
    if any(c not in digits for c in part):
        return None
    return int(part, radix)


def _ipv6(address: ipaddress.IPv6Address) -> str:
    """An IPv6 address as the URL standard writes it: lowercase hex, the first longest run of zeros as ::."""
    pieces = [int.from_bytes(address.packed[i : i + 2], "big") for i in range(0, 16, 2)]
    best, best_len, i = -1, 1, 0
    while i < 8:
        j = i
        while j < 8 and pieces[j] == 0:
            j += 1
        if j - i > best_len:
            best, best_len = i, j - i
        i = j + 1 if j == i else j
    if best < 0:
        return ":".join(f"{p:x}" for p in pieces)
    head = ":".join(f"{p:x}" for p in pieces[:best])
    tail = ":".join(f"{p:x}" for p in pieces[best + best_len :])
    return f"{head}::{tail}"


def _without_dot_segments(path: str) -> str:
    """Resolves . and .. in a path, %2e included, as a URL parser does before the path is used."""
    kept: list[str] = []
    for segment in path.split("/")[1:]:
        dots = segment.lower().replace("%2e", ".")
        if dots == ".":
            continue
        if dots == "..":
            if kept:
                kept.pop()
            continue
        kept.append(segment)
    return "/" + "/".join(kept) if kept else ""


def _encode_slug(slug: object) -> str:
    rule = 'slug must be a city slug, for example "amsterdam".'
    if not isinstance(slug, str):
        raise TypeError(f"{rule} Got {type(slug).__name__}.")
    s = _trim(slug)
    # "." and ".." would be read as path steps and reach another endpoint.
    if s in ("", ".", ".."):
        raise ValueError(f"{rule} Got {json.dumps(slug, ensure_ascii=False)}.")
    # The characters encodeURIComponent leaves alone, so both SDKs send the same path.
    return urllib.parse.quote(s, safe="!'()*~")


def _user_agent(extra: object) -> str:
    python = ".".join(str(n) for n in sys.version_info[:3])
    agent = f"socials-homes-python/{VERSION} python/{python}"
    if extra is None or extra == "":
        return agent
    if not isinstance(extra, str):
        raise TypeError('user_agent must be a string, for example "my-agent/1.2".')
    if not re.fullmatch(r"[\x20-\x7e]+", extra):
        raise ValueError('user_agent must be printable ASCII on one line, for example "my-agent/1.2".')
    return f"{agent} {extra}"


_NOT_JSON = object()


def _parse_json(raw: bytes) -> object:
    # Decoded as fetch's text() does: UTF-8, bad bytes replaced, a BOM dropped.
    text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text, parse_constant=_not_json_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _finite_float(text: str) -> float:
    # 1e999 parses to inf, which JSON cannot carry back out; an answer holding one is not the API's.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is too large for a number")
    return value


def _not_json_constant(name: str) -> object:
    # NaN, Infinity and -Infinity are Python's extension; JSON.parse refuses them.
    raise ValueError(f"{name} is not JSON")


def _is_problem(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    return (
        isinstance(value.get("code"), str)
        and isinstance(status, (int, float))
        and not isinstance(status, bool)
        and isinstance(value.get("detail"), str)
    )


def _retry_after(header: Optional[str], problem: Optional[Problem]) -> Optional[int]:
    """Seconds to wait, from Retry-After (seconds or an HTTP date) or the problem body. Never negative."""
    h = _trim(header) if header else ""
    if h:
        if re.fullmatch(r"[0-9]+", h):
            digits = h.lstrip("0") or "0"
            # Past 18 digits it is longer than any wait; capped so no Python's digit limit applies.
            return int(digits) if len(digits) <= 18 else sys.maxsize
        at = _http_date(h)
        if at is not None:
            return max(0, math.ceil(at - time.time()))
    seconds = problem.get("retry_after_seconds") if problem is not None else None
    if isinstance(seconds, bool):
        return None
    if isinstance(seconds, int):
        return max(0, min(seconds, sys.maxsize))
    if isinstance(seconds, float) and math.isfinite(seconds):
        return max(0, math.ceil(seconds))
    return None


def _http_date(text: str) -> Optional[float]:
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _invalid_response(url: str, status: int, what: str) -> SocialsHomesError:
    return SocialsHomesError(f"{url} answered {what}.", status=status, code="invalid_response", url=url)


__all__ = ["DEFAULT_BASE_URL", "ClientErrorCode", "Opener", "SocialsHomes", "SocialsHomesError"]

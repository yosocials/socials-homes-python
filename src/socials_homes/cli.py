"""socials-homes, the Socials Homes API from the terminal."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any, Callable, Optional, Union, cast

from ._version import VERSION
from .client import DEFAULT_BASE_URL, Opener, SocialsHomes, SocialsHomesError, _trim
from .types import ApiIndex, City, CitySupply, PlansResponse

EXIT_OK = 0
"""Done."""
EXIT_FAILED = 1
"""The API or the network failed."""
EXIT_USAGE = 2
"""The command line is wrong."""

USAGE = f"""socials-homes {VERSION}, the Socials Homes API from the terminal. Read-only, no API key.

Usage
  socials-homes cities [--country <code or name>]   every covered city
  socials-homes city <slug>                          one city and its links
  socials-homes supply <slug> [--budget <eur>]       live rental listings in a city now
  socials-homes plans                                plans, prices and renewal
  socials-homes info                                 the API index: endpoints, limits, docs

Options
  --json             print the API's JSON answer instead of text
  --base-url <url>   another API origin (default {DEFAULT_BASE_URL})
  -h, --help         this help
  -v, --version      the version

Examples
  socials-homes cities --country NL
  socials-homes supply amsterdam --budget 900
  socials-homes plans --json

Docs: {DEFAULT_BASE_URL}/developers"""

COMMANDS = ("cities", "city", "supply", "plans", "info", "help")


@dataclass(frozen=True)
class CliIo:
    """Where the CLI writes. Each call is one block of text; the writer adds the newline."""

    stdout: Callable[[str], None]
    stderr: Callable[[str], None]
    opener: Optional[Opener] = None
    """Injected in tests. Default urllib."""


def main(argv: Optional[Sequence[str]] = None, io: Optional[CliIo] = None) -> int:
    """Runs one command. Returns 0 done, 1 the API or the network failed, 2 the command line is wrong."""
    args = list(sys.argv[1:] if argv is None else argv)
    io = io if io is not None else CliIo(stdout=_writer("stdout"), stderr=_writer("stderr"))
    try:
        values, positionals = _parse_args(args)
    except _UsageError as err:
        return _usage_error(io, str(err))

    if values.get("version"):
        io.stdout(VERSION)
        return EXIT_OK
    command = positionals[0] if positionals else None
    rest = positionals[1:]
    if values.get("help") or command is None or command == "help":
        io.stdout(USAGE)
        return EXIT_OK
    if command not in COMMANDS:
        return _usage_error(io, f'Unknown command "{command}".')

    country = cast(Optional[str], values.get("country"))
    budget = cast(Optional[str], values.get("budget"))
    needs_slug = command in ("city", "supply")
    if needs_slug and len(rest) != 1:
        return _usage_error(io, f"{command} takes one city slug, for example: socials-homes {command} amsterdam")
    if not needs_slug and rest:
        return _usage_error(io, f'{command} takes no arguments, got "{" ".join(rest)}".')
    if country is not None and command != "cities":
        return _usage_error(io, "--country only works with cities.")
    if country is not None and _trim(country) == "":
        return _usage_error(io, "--country needs a country code or name, for example --country NL.")
    if budget is not None and command != "supply":
        return _usage_error(io, "--budget only works with supply.")
    budget_max_eur: Optional[int] = None
    if budget is not None:
        whole = "--budget is a whole number of EUR, for example --budget 900."
        if not re.fullmatch(r"[0-9]+", budget):
            return _usage_error(io, whole)
        digits = budget.lstrip("0") or "0"
        # Past 18 digits it is no budget, and no Python's digit limit is reached.
        if len(digits) > 18:
            return _usage_error(io, whole)
        budget_max_eur = int(digits)

    try:
        base_url = cast(Optional[str], values.get("base-url"))
        client = SocialsHomes(base_url, opener=io.opener, user_agent=f"socials-homes-cli/{VERSION}")
    except (ValueError, TypeError) as err:
        return _usage_error(io, str(err))

    slug = rest[0] if rest else ""
    try:
        answer = _fetch(command, client, slug, country, budget_max_eur)
    except SocialsHomesError as err:
        io.stderr(printable(_describe_error(err)))
        # The API rejecting a parameter is a command-line mistake too.
        return EXIT_USAGE if err.code == "invalid_parameter" else EXIT_FAILED
    except (ValueError, TypeError) as err:
        # The client raises these for a bad argument, before any request.
        return _usage_error(io, str(err))
    except Exception as err:  # noqa: BLE001 - reported like any other failure, without a traceback
        io.stderr(printable(f"socials-homes: {err}"))
        return EXIT_FAILED

    try:
        # JSON escapes control characters itself; text is cleaned of them below.
        text = _json(answer) if values.get("json") else _render(command, answer, country)
    except Exception as err:  # noqa: BLE001 - an answer of a shape this version cannot print
        problem = f"{type(err).__name__}: {err}"
        io.stderr(printable(f"socials-homes: The answer could not be printed ({problem}). Try --json."))
        return EXIT_FAILED
    io.stdout(text if values.get("json") else printable(text))
    return EXIT_OK


def entry() -> None:
    """The socials-homes console script."""
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        # The reader went away, as with | head. Stop quietly.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        code = EXIT_FAILED
    sys.exit(code)


_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Strips terminal control sequences an answer could carry, keeping newlines and tabs."""
    return _CONTROL.sub("", text)


class _UsageError(Exception):
    pass


# Name, type and short letter of each option, as given to util.parseArgs in the JavaScript CLI.
_OPTIONS: dict[str, tuple[str, Optional[str]]] = {
    "json": ("boolean", None),
    "country": ("string", None),
    "budget": ("string", None),
    "base-url": ("string", None),
    "help": ("boolean", "h"),
    "version": ("boolean", "v"),
}
_SHORT = {short: name for name, (_, short) in _OPTIONS.items() if short}


def _parse_args(args: Sequence[str]) -> tuple[dict[str, Union[str, bool]], list[str]]:
    """Node's util.parseArgs in strict mode with positionals allowed: the same tokens, the same errors."""
    values: dict[str, Union[str, bool]] = {}
    positionals: list[str] = []
    remaining = list(args)
    while remaining:
        arg = remaining.pop(0)
        if arg == "--":
            positionals.extend(remaining)
            break
        if len(arg) > 2 and arg.startswith("--"):
            # Node asks for an "=" from the fourth character on, then splits at the first one.
            if "=" not in arg[3:]:
                name, value = arg[2:], None
                if _OPTIONS.get(name, ("", None))[0] == "string" and remaining:
                    value = remaining.pop(0)
                _store(values, name, arg, value, inline=False)
            else:
                eq = arg.index("=")
                _store(values, arg[2:eq], arg[:eq], arg[eq + 1 :], inline=True)
        elif len(arg) > 2 and arg.startswith("-"):
            # -hv is -h -v, read again from the front, so "-h-" ends in "--". No short option takes a value.
            remaining[:0] = [f"-{letter}" for letter in arg[1:]]
        elif len(arg) == 2 and arg.startswith("-"):
            _store(values, _SHORT.get(arg[1], f"\0{arg[1]}"), arg, None, inline=False)
        else:
            positionals.append(arg)
    return values, positionals


def _store(values: dict[str, Union[str, bool]], name: str, raw: str, value: Optional[str], *, inline: bool) -> None:
    if name not in _OPTIONS:
        raise _UsageError(
            f"Unknown option '{raw}'. To specify a positional argument starting with a '-', "
            f"place it at the end of the command after '--', as in '-- {json.dumps(raw, ensure_ascii=False)}"
        )
    kind, short = _OPTIONS[name]
    shown = f"-{short}, --{name}" if short else f"--{name}"
    if kind == "string" and value is None:
        raise _UsageError(f"Option '{shown} <value>' argument missing")
    if kind == "boolean" and value is not None:
        raise _UsageError(f"Option '{shown}' does not take an argument")
    if not inline and value is not None and len(value) > 1 and value.startswith("-"):
        raise _UsageError(
            f"Option '{raw}' argument is ambiguous.\nDid you forget to specify the option argument for '{raw}'?\n"
            f"To specify an option argument starting with a dash use '{raw}=-XYZ'."
        )
    values[name] = True if value is None else value


def _fetch(command: str, client: SocialsHomes, slug: str, country: Optional[str], budget_max_eur: Optional[int]) -> Any:
    if command == "cities":
        return list(client.cities(country=country))
    if command == "city":
        return client.get_city(slug)
    if command == "supply":
        return client.get_city_supply(slug, budget_max_eur=budget_max_eur)
    if command == "plans":
        return client.list_plans()
    return client.get_index()


def _render(command: str, answer: Any, country: Optional[str]) -> str:
    if command == "cities":
        return _cities_text(answer, country)
    if command == "city":
        return _city_text(answer)
    if command == "supply":
        return _supply_text(answer)
    if command == "plans":
        return _plans_text(answer)
    return _info_text(answer)


# Text output. Plain, aligned, no colour, so it pipes cleanly.


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _count(n: Union[int, float]) -> str:
    """A number as en-GB writes it: 1,187."""
    if isinstance(n, int):
        return f"{n:,}"
    return f"{n:,.3f}".rstrip("0").rstrip(".")


_CENTS = Decimal("0.01")
_WIDE = Context(prec=400)


def _eur(n: float) -> str:
    if abs(n) >= 1e21:
        # toFixed gives up on fixed notation here and prints the number as it is.
        return f"EUR {float(n)!r}"
    # The exact value of the float, rounded half up: what toFixed(2) prints.
    return f"EUR {Decimal(n + 0).quantize(_CENTS, rounding=ROUND_HALF_UP, context=_WIDE)}"


def _per(seconds: int) -> str:
    if seconds == 60:
        return "minute"
    if seconds == 3600:
        return "hour"
    if seconds == 86_400:
        return "day"
    return f"{_count(seconds)} seconds"


def _width(text: str) -> int:
    """String.length: UTF-16 code units, which is what padEnd pads to."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _table(rows: list[list[str]]) -> str:
    widths = [max(_width(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell if i == len(r) - 1 else cell + " " * (widths[i] - _width(cell)) for i, cell in enumerate(r))
        for r in rows
    )


def _cities_text(cities: list[City], country: Optional[str]) -> str:
    if not cities:
        return f'No covered city in "{country}".' if country else "No covered city."
    head = f"{_count(len(cities))} {'city' if len(cities) == 1 else 'cities'}"
    if country:
        head += f" in {cities[0]['country_name']}"
    rows = [["SLUG", "NAME", "COUNTRY"]] + [
        [c["slug"], c["name"], f"{c['country']} {c['country_name']}"] for c in cities
    ]
    return f"{head}\n\n{_table(rows)}"


def _city_text(c: City) -> str:
    links = c["links"]
    return "\n".join(
        [
            f"{c['name']}, {c['country_name']} ({c['slug']})",
            "",
            _table([["Live counts", links["supply"]], ["City page", links["page"]], ["Sign up", links["signup"]]]),
        ]
    )


def _supply_text(s: CitySupply) -> str:
    lines = [f"{s['city']['name']}, {s['city']['country_name']}", ""]
    a = s["supply"]
    if not a["available"]:
        lines += ["Live counts are unavailable right now. That is not zero.", a["reason"]]
    else:
        lines.append(f"Live listings: {_count(a['live_listings'])}")
        budget, within = a["budget_max_eur"], a["listings_within_budget"]
        if budget is not None and within is not None:
            level = f" ({a['level']})" if a["level"] else ""
            lines.append(f"Within EUR {_count(budget)} a month: {_count(within)}{level}")
        if a["suggested_budget_eur"] is not None:
            lines.append(f"A budget of EUR {_count(a['suggested_budget_eur'])} would reach about 30 listings.")
    lines += ["", s["note"], f"Sign up: {s['signup_url']}"]
    return "\n".join(lines)


def _plans_text(r: PlansResponse) -> str:
    # billing is the API's own sentence on how the plan charges and renews.
    rows = [["PLAN", "NAME", "PRICE", "BILLING"]] + [
        [p["id"], p["name"], _eur(p["price_eur"]), p["billing"]] for p in r["data"]
    ]
    return "\n".join(
        [
            _table(rows),
            "",
            r["prices_note"],
            r["cancellation"],
            f"Pricing: {r['pricing_url']}",
            f"Terms: {r['terms_url']}",
        ]
    )


def _info_text(i: ApiIndex) -> str:
    rows = [["ENDPOINT", "SUMMARY"]] + [[f"{e['method']} {e['path']}", e["summary"]] for e in i["endpoints"]]
    limits = [
        f"Limit {lim['name']}: {_count(lim['limit'])} requests per {_per(lim['window_seconds'])}. {lim['applies_to']}."
        for lim in i["rate_limits"]
    ]
    return "\n".join(
        [
            f"{i['name']} v{i['version']} (spec {i['spec_version']}, released {i['released']})",
            i["authentication"]["description"],
            "",
            _table(rows),
            "",
            *limits,
            "",
            f"Docs: {i['documentation_url']}",
            f"OpenAPI: {i['openapi_url']}",
            *[f"MCP: {m['name']}, {m['url']}" for m in i["mcp_servers"]],
        ]
    )


def _describe_error(err: SocialsHomesError) -> str:
    lines = [f"socials-homes: {err.message}"]
    p: dict[str, Any] = dict(err.problem) if err.problem is not None else {}
    # Each parameter on its own line, unless the detail already says exactly that.
    errors = p.get("errors")
    for e in errors if isinstance(errors, list) else []:
        if isinstance(e, dict) and e.get("message") != err.message:
            lines.append(f"  {e.get('parameter')}: {e.get('message')}")
    suggestions = [s for s in p.get("suggestions") or [] if isinstance(s, dict)]
    if suggestions:
        names = ", ".join(f"{s.get('slug')} ({s.get('name')}, {s.get('country')})" for s in suggestions)
        lines.append(f"Did you mean: {names}?")
    if err.retry_after_seconds is not None:
        lines.append(f"Try again in {err.retry_after_seconds} seconds.")
    elif err.resolution:
        lines.append(err.resolution)
    return "\n".join(lines)


def _usage_error(io: CliIo, message: str) -> int:
    io.stderr(printable(f"socials-homes: {message}\nRun socials-homes --help for usage."))
    return EXIT_USAGE


def _writer(name: str) -> Callable[[str], None]:
    """Writes UTF-8 whatever the locale, as Node does, so a pipe gets the API's text as it is."""

    def write(text: str) -> None:
        stream = getattr(sys, name)
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            stream.write(f"{text}\n")
            stream.flush()
            return
        stream.flush()
        # A lone surrogate (only possible inside a JSON string) becomes its JSON escape.
        buffer.write(f"{text}\n".encode("utf-8", errors="backslashreplace"))
        buffer.flush()

    return write


__all__ = ["COMMANDS", "EXIT_FAILED", "EXIT_OK", "EXIT_USAGE", "USAGE", "CliIo", "entry", "main", "printable"]

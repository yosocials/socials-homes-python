# Socials Homes Python SDK and CLI

The official Python client for the [Socials Homes API](https://www.socials.homes/developers): the cities Socials Homes covers, its plans and prices in EUR, and live rental listing counts per city and budget.

The API is public and read-only. There is no API key, no account and nothing to configure. This package has no runtime dependencies and only ever sends GET requests.

## CLI

```bash
pipx run socials-homes supply amsterdam --budget 900
# or
uvx socials-homes supply amsterdam --budget 900
```

```
Amsterdam, Netherlands

Live listings: 1,181
Within EUR 900 a month: 192 (ok)

These are the listings Socials Homes counts in its own copy of the market; some housing sites are not counted here, so alerts can bring more. Counts refresh about every 10 minutes and change as homes are let.
Sign up: https://www.socials.homes/signup?city=amsterdam
```

The counts are live, so yours will differ.

| Command | What it prints |
| --- | --- |
| `socials-homes cities [--country NL]` | Every covered city, or the ones in a country (ISO code or English name) |
| `socials-homes city <slug>` | One city and its links |
| `socials-homes supply <slug> [--budget <eur>]` | Live rental listings in the city now, and how many are within the budget |
| `socials-homes plans` | Every plan with its price and how it bills and renews |
| `socials-homes info` | The API index: endpoints, rate limits, docs, MCP servers |

Add `--json` to any command for the API's JSON answer. Exit codes: `0` done, `1` the API or the network failed, `2` the command line is wrong. Install it with `pipx install socials-homes` or `uv tool install socials-homes` to drop the `pipx run`; `python -m socials_homes` runs it too.

## SDK

```bash
pip install socials-homes
```

```python
from socials_homes import SocialsHomes

sh = SocialsHomes()

supply = sh.get_city_supply("amsterdam", budget_max_eur=900)["supply"]
if supply["available"]:
    print(supply["live_listings"], supply["listings_within_budget"])

for city in sh.cities(country="NL"):
    print(city["slug"], city["name"])

plans = sh.list_plans()
```

Python 3.9 or later, standard library only. Types are included: every answer is a plain `dict`, described by a `TypedDict` (`City`, `CitySupply`, `PlansResponse` and the rest) that mypy and your editor read.

| Method | Endpoint | Returns |
| --- | --- | --- |
| `get_index()` | `GET /api/v1` | Every endpoint, the OpenAPI document, the docs, the limits |
| `list_cities(country=, limit=, cursor=)` | `GET /api/v1/cities` | One page: `{"data", "next_cursor"}` |
| `cities(country=, page_size=)` | `GET /api/v1/cities`, every page | An iterator over every city |
| `get_city(slug)` | `GET /api/v1/cities/{slug}` | The city |
| `get_city_supply(slug, budget_max_eur=)` | `GET /api/v1/cities/{slug}/supply` | `{"city", "supply", "note", "signup_url"}` |
| `list_plans()` | `GET /api/v1/plans` | Plans, prices, renewal, cancellation and the VIP guarantee |

Field names are the API's own, in snake_case, exactly as the [OpenAPI document](https://www.socials.homes/openapi.json) describes them. Within v1 the API only adds endpoints, optional parameters and fields, so read what you need and ignore what you do not know.

When live counts cannot be read, `supply["available"]` is `False` with a `reason`. That is never zero listings.

### Errors

A failed call raises a `SocialsHomesError`, whether the API answered with an error or the request never got an answer. A bad argument is not one: an empty slug, a malformed `base_url` or a client option out of range (a negative `timeout`, say) raises `ValueError`, and a value of the wrong type raises `TypeError`, before any request. Ranges the API checks itself, such as `limit` from 1 to 200, come back from the API as `invalid_parameter`.

```python
from socials_homes import SocialsHomesError

try:
    sh.get_city("den_haag")
except SocialsHomesError as err:
    if err.code == "city_not_found":
        print(err.problem["suggestions"])  # [{"slug": "the_hague", ...}]
```

| Attribute | Meaning |
| --- | --- |
| `status` | The HTTP status, or `0` when no answer arrived |
| `code` | The API's code (`invalid_parameter`, `not_found`, `city_not_found`, `rate_limited`, `internal_error`), or `timeout`, `connection_error`, `invalid_response` from the client |
| `message` | The API's `detail`, a sentence you can show. `str(err)` is the same |
| `resolution` | The API's hint on how to fix the request |
| `problem` | The full [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem, with `errors` per parameter and `suggestions` for a mistyped city |
| `retry_after_seconds` | On a 429, how long the API asked you to wait |
| `url` | The URL that was called |

When the transport raised an exception, a refused connection for one, it is `err.__cause__`.

### Retries and timeouts

Every call is a GET, so retrying is safe. A 429 waits out `Retry-After` (seconds or a date); a 500, 502, 503 or 504, a timeout or a network failure backs off from 0.5 seconds, doubling each time. Two retries by default. A `Retry-After` longer than 30 seconds is raised at once rather than waited out silently. Any other 4xx is never retried.

```python
sh = SocialsHomes(
    timeout=15.0,  # seconds per attempt, from connecting to the last byte
    max_retries=2,
    max_retry_delay=30.0,  # the longest Retry-After to wait out
    retry_base_delay=0.5,  # the first backoff, doubled each time
    user_agent="my-agent/1.0",
)
```

A `KeyboardInterrupt` is never caught or retried. The client keeps no connection or state, so one instance can be shared across threads. Proxies set in `HTTPS_PROXY` and the like are used. For anything else, pass `opener=`: any object with the `open(request, timeout=...)` of `urllib.request.OpenerDirector`.

### Rate limits

300 requests a minute per IP address across the API, and 600 an hour for the supply endpoint on top. Answers carry the IETF `RateLimit` headers, so you can see how much is left. See [the docs](https://www.socials.homes/developers#rate-limits).

## What it sends

A GET to `https://www.socials.homes/api/v1/...`, an `Accept: application/json` header and a `User-Agent` such as `socials-homes-python/1.0.0 python/3.13.7`. The client sets no other header: no credentials, no cookies, no telemetry. Point it elsewhere with `base_url`, an http or https origin with an optional path; write an international host name in its `xn--` form. Redirects are followed to http and https only. Text the CLI prints is cleaned of terminal control characters.

## Other ways in

- JavaScript and TypeScript: `npm install socials-homes`, the [JavaScript SDK and CLI](https://github.com/yosocials/socials-homes-js)
- REST: [developer docs](https://www.socials.homes/developers), [OpenAPI 3.1](https://www.socials.homes/openapi.json)
- MCP: `https://www.socials.homes/mcp` (product) and `https://www.socials.homes/mcp/docs` (docs), public and read-only, listed in the [official MCP registry](https://registry.modelcontextprotocol.io/v0/servers?search=homes.socials)
- Agent skills and plugin manifests: [yosocials/socials-homes-agents](https://github.com/yosocials/socials-homes-agents)

## Development

```bash
PYTHONPATH=src python -m unittest                       # the suite, offline
SOCIALS_HOMES_LIVE=1 PYTHONPATH=src python -m unittest tests.test_live   # against the real API, read-only
uvx mypy --strict src
uvx ruff check
uv build
```

## License

The code in this repository is MIT licensed. The data the API serves, and use of the API, fall under the [Socials Homes terms of service](https://www.socials.homes/terms). Socials BV, Brussels. Questions: [hello@socials.homes](mailto:hello@socials.homes).

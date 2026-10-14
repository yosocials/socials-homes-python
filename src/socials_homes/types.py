"""Response shapes of the Socials Homes API v1.

Field for field as the OpenAPI document at https://www.socials.homes/openapi.json
describes them, nullability included. Field names stay snake_case, exactly as the
API sends them, and every answer is a plain dict: these TypedDicts only describe it.

Within v1 the API only adds: new endpoints, new optional parameters and new
fields. Code that reads these types should ignore keys it does not know rather
than reject them.
"""

from typing import Literal, Optional, TypedDict, Union


class CityLinks(TypedDict):
    self: str
    """This city in the API."""
    supply: str
    """Its live listing counts in the API."""
    page: str
    """Its page on the website."""
    signup: str
    """The signup funnel with this city filled in."""


class City(TypedDict):
    slug: str
    """Lowercase, underscores between words, for example "the_hague"."""
    name: str
    country: str
    """ISO 3166-1 alpha-2 code, for example "NL"."""
    country_name: str
    links: CityLinks


class CityList(TypedDict):
    data: list[City]
    next_cursor: Optional[str]
    """Pass back as cursor for the next page. None on the last page."""


class CityResponse(TypedDict):
    data: City


SupplyLevel = Literal["critical", "low", "ok"]


class SupplyAvailable(TypedDict):
    available: Literal[True]
    live_listings: int
    """Live listings counted in this city right now, at any price."""
    budget_max_eur: Optional[int]
    """The budget asked for, or None when none was given."""
    listings_within_budget: Optional[int]
    """Live listings at or under the budget, including listings without a stated price. None without a budget."""
    level: Optional[SupplyLevel]
    """critical (under 5 listings), low (5 to 15) or ok (16 or more). None without a budget."""
    suggested_budget_eur: Optional[int]
    """A budget that would reach about 30 listings when the market is thin, rounded up to 50. Otherwise None."""


class SupplyUnavailable(TypedDict):
    available: Literal[False]
    reason: str
    """Why, in a sentence to pass on as written. Unavailable never means zero."""


Supply = Union[SupplyAvailable, SupplyUnavailable]


class CitySupply(TypedDict):
    city: City
    supply: Supply
    note: str
    """What the counts cover and how often they refresh."""
    signup_url: str


class SupplyResponse(TypedDict):
    data: CitySupply


class Guarantee(TypedDict):
    summary: str
    url: str


class Plan(TypedDict):
    id: str
    """Today explorer, seeker, vip-3-months or vip-lifetime. New plans may appear within v1."""
    name: str
    path: str
    """on_your_own or with_an_expert today. An open set."""
    currency: Literal["EUR"]
    price_eur: float
    """What checkout charges today, in EUR."""
    billing: str
    """How the charge recurs, in words a person reads."""
    billing_interval: str
    """Today "month", "2 months", "3 months, then monthly" or "one-time". Read billing for the words."""
    auto_renews: bool
    """Whether the plan renews automatically until cancelled."""
    renewal_price_eur: Optional[float]
    """What each renewal charges, in EUR. None for a one-time payment."""
    renews_every: Optional[str]
    """Today "month" or "2 months". None for a one-time payment."""
    per_month_eur: Optional[float]
    """The per-month figure for the first term. None for Lifetime."""
    includes: list[str]
    guarantee: Optional[Guarantee]


class VipGuarantee(TypedDict):
    summary: str
    conditions: list[str]
    how_to_claim: str
    url: str


class PlansResponse(TypedDict):
    data: list[Plan]
    currency: Literal["EUR"]
    prices_note: str
    cancellation: str
    refunds: str
    vip_guarantee: VipGuarantee
    pricing_url: str
    terms_url: str


class ApiAuthentication(TypedDict):
    required: Literal[False]
    """Always False: there is no authentication."""
    description: str


class ApiEndpoint(TypedDict):
    operation_id: str
    method: Literal["GET"]
    path: str
    url: str
    summary: str


class ApiRateLimit(TypedDict):
    name: str
    limit: int
    window_seconds: int
    applies_to: str


class ApiMcpServer(TypedDict):
    name: str
    url: str
    server_card: str


class ApiIndex(TypedDict):
    name: str
    version: Literal["1"]
    spec_version: str
    released: str
    description: str
    authentication: ApiAuthentication
    read_only: Literal[True]
    documentation_url: str
    documentation_markdown_url: str
    openapi_url: str
    openapi_yaml_url: str
    api_catalog_url: str
    endpoints: list[ApiEndpoint]
    rate_limits: list[ApiRateLimit]
    mcp_servers: list[ApiMcpServer]
    contact: str
    terms_url: str


ProblemCode = Literal[
    "invalid_parameter",
    "not_found",
    "city_not_found",
    "method_not_allowed",
    "rate_limited",
    "internal_error",
]
"""The machine-readable error codes. New codes may be added within v1, so handle an unknown one by its status."""

# "in" is a Python keyword, so this one takes the functional form.
ParameterError = TypedDict(
    "ParameterError",
    {"parameter": str, "in": Literal["query", "path"], "message": str},
)


class CitySuggestion(TypedDict):
    slug: str
    name: str
    country: str
    country_name: str


class _ProblemRequired(TypedDict):
    type: str
    title: str
    status: int
    detail: str
    code: Union[ProblemCode, str]
    resolution: str


class Problem(_ProblemRequired, total=False):
    """An RFC 9457 problem, the body of every error the API answers."""

    instance: str
    errors: list[ParameterError]
    suggestions: list[CitySuggestion]
    allowed_methods: list[Literal["GET", "HEAD", "OPTIONS"]]
    retry_after_seconds: int


__all__ = [
    "ApiAuthentication",
    "ApiEndpoint",
    "ApiIndex",
    "ApiMcpServer",
    "ApiRateLimit",
    "City",
    "CityLinks",
    "CityList",
    "CityResponse",
    "CitySuggestion",
    "CitySupply",
    "Guarantee",
    "ParameterError",
    "Plan",
    "PlansResponse",
    "Problem",
    "ProblemCode",
    "Supply",
    "SupplyAvailable",
    "SupplyLevel",
    "SupplyResponse",
    "SupplyUnavailable",
    "VipGuarantee",
]

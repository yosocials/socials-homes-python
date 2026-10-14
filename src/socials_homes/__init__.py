"""The official Python SDK and CLI for the Socials Homes API.

Public and read-only: no API key, no account, nothing to configure.

    from socials_homes import SocialsHomes

    sh = SocialsHomes()
    supply = sh.get_city_supply("amsterdam", budget_max_eur=900)["supply"]

Docs: https://www.socials.homes/developers
"""

from ._version import VERSION
from .client import DEFAULT_BASE_URL, ClientErrorCode, Opener, SocialsHomes, SocialsHomesError
from .types import (
    ApiAuthentication,
    ApiEndpoint,
    ApiIndex,
    ApiMcpServer,
    ApiRateLimit,
    City,
    CityLinks,
    CityList,
    CityResponse,
    CitySuggestion,
    CitySupply,
    Guarantee,
    ParameterError,
    Plan,
    PlansResponse,
    Problem,
    ProblemCode,
    Supply,
    SupplyAvailable,
    SupplyLevel,
    SupplyResponse,
    SupplyUnavailable,
    VipGuarantee,
)

__version__ = VERSION

__all__ = [
    "DEFAULT_BASE_URL",
    "VERSION",
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
    "ClientErrorCode",
    "Guarantee",
    "Opener",
    "ParameterError",
    "Plan",
    "PlansResponse",
    "Problem",
    "ProblemCode",
    "SocialsHomes",
    "SocialsHomesError",
    "Supply",
    "SupplyAvailable",
    "SupplyLevel",
    "SupplyResponse",
    "SupplyUnavailable",
    "VipGuarantee",
    "__version__",
]

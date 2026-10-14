"""Checks the TypedDicts against an OpenAPI document, and API answers against the TypedDicts."""

from __future__ import annotations

import typing
from typing import Any, Literal, Union

import socials_homes as sh

# Each named schema, and each object the spec writes inline, and the TypedDict for it.
NAMED: dict[str, Any] = {
    "City": sh.City,
    "CityList": sh.CityList,
    "CityResponse": sh.CityResponse,
    "SupplyAvailable": sh.SupplyAvailable,
    "SupplyUnavailable": sh.SupplyUnavailable,
    "Supply": sh.Supply,
    "SupplyResponse": sh.SupplyResponse,
    "Guarantee": sh.Guarantee,
    "Plan": sh.Plan,
    "VipGuarantee": sh.VipGuarantee,
    "PlansResponse": sh.PlansResponse,
    "ApiIndex": sh.ApiIndex,
    "ParameterError": sh.ParameterError,
    "CitySuggestion": sh.CitySuggestion,
    "Problem": sh.Problem,
}
INLINE: dict[str, Any] = {
    "City.links": sh.CityLinks,
    "SupplyResponse.data": sh.CitySupply,
    "ApiIndex.authentication": sh.ApiAuthentication,
    "ApiIndex.endpoints[]": sh.ApiEndpoint,
    "ApiIndex.rate_limits[]": sh.ApiRateLimit,
    "ApiIndex.mcp_servers[]": sh.ApiMcpServer,
}
# Enums the spec calls open ("a new one may be added"): typed as the known values or any string.
OPEN_SETS = {"Problem.code"}
SCALARS: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool}


def expected_hint(schema: dict[str, Any], where: str) -> Any:
    """The Python type an OpenAPI schema describes, as types.py should spell it."""
    if "$ref" in schema:
        return NAMED[schema["$ref"].rsplit("/", 1)[-1]]
    if "oneOf" in schema:
        return Union[tuple(expected_hint(s, where) for s in schema["oneOf"])]
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    options: list[Any] = []
    for kind in kinds:
        if kind == "null":
            options.append(type(None))
        elif "const" in schema:
            options.append(Literal[schema["const"]])
        elif "enum" in schema:
            literal = Literal[tuple(v for v in schema["enum"] if v is not None)]
            options.append(Union[literal, str] if where in OPEN_SETS else literal)
        elif kind == "array":
            options.append(list[expected_hint(schema["items"], f"{where}[]")])
        elif kind == "object":
            options.append(INLINE[where])
        else:
            options.append(SCALARS[kind])
    return Union[tuple(options)] if len(options) > 1 else options[0]


def mismatches(spec: dict[str, Any]) -> list[str]:
    """Every difference between the spec's component schemas and types.py: fields, required keys, types, nullability."""
    found: list[str] = []
    schemas = spec["components"]["schemas"]
    found += [f"{name}: in the spec, not in NAMED" for name in sorted(set(schemas) - set(NAMED))]

    def compare(schema: dict[str, Any], td: Any, where: str) -> None:
        hints = typing.get_type_hints(td)
        props = schema.get("properties", {})
        if set(props) != set(hints):
            found.append(f"{where}: fields {sorted(set(props) ^ set(hints))} differ")
        if set(schema.get("required", [])) != set(td.__required_keys__):
            found.append(f"{where}: required {sorted(schema.get('required', []))} != {sorted(td.__required_keys__)}")
        for key, prop in props.items():
            if key not in hints:
                continue
            want = expected_hint(prop, f"{where}.{key}")
            if want != hints[key]:
                found.append(f"{where}.{key}: spec says {want}, types.py says {hints[key]}")
            # Walk into inline objects too.
            inner = prop.get("items", prop)
            path = f"{where}.{key}[]" if "items" in prop else f"{where}.{key}"
            if inner.get("type") == "object" and path in INLINE:
                compare(inner, INLINE[path], path)

    for name, schema in schemas.items():
        if name not in NAMED:
            continue
        if "oneOf" in schema:
            if expected_hint(schema, name) != NAMED[name]:
                found.append(f"{name}: spec says {expected_hint(schema, name)}, types.py says {NAMED[name]}")
            continue
        compare(schema, NAMED[name], name)
    return found


def conforms(value: Any, hint: Any, where: str = "$") -> list[str]:
    """Where an answer differs from its type. Keys the type does not know are allowed: v1 only adds."""
    origin = typing.get_origin(hint)
    if origin is Union:
        options = [conforms(value, option, where) for option in typing.get_args(hint)]
        return [] if any(not o for o in options) else [f"{where}: {value!r} fits none of {hint}"]
    if origin is Literal:
        return (
            []
            if any(value == v and type(value) is type(v) for v in typing.get_args(hint))
            else [f"{where}: {value!r} not in {hint}"]
        )
    if origin is list:
        if not isinstance(value, list):
            return [f"{where}: not a list"]
        (item,) = typing.get_args(hint)
        return [p for i, v in enumerate(value) for p in conforms(v, item, f"{where}[{i}]")]
    if hint is type(None):
        return [] if value is None else [f"{where}: not null"]
    if hint is float:
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) else [f"{where}: not a number"]
    if hint is int:
        return [] if isinstance(value, int) and not isinstance(value, bool) else [f"{where}: not an integer"]
    if hint in (str, bool):
        return [] if isinstance(value, hint) else [f"{where}: not a {hint.__name__}"]
    if hasattr(hint, "__required_keys__"):
        if not isinstance(value, dict):
            return [f"{where}: not an object"]
        problems = [f"{where}.{k}: missing" for k in sorted(hint.__required_keys__) if k not in value]
        for key, inner in typing.get_type_hints(hint).items():
            if key in value:
                problems += conforms(value[key], inner, f"{where}.{key}")
        return problems
    raise TypeError(f"no rule for {hint}")

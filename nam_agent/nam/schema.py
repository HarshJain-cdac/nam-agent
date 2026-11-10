"""A small JSON-schema validator: the subset the NAM API contract uses.

Every request into the NAM harness and every response out of it is checked
against a schema in nam_agent/nam/schemas/. The full JSON-schema spec would need a
new dependency (hard rule 3), and the contract only uses a handful of
keywords, so this validates exactly those and rejects a schema that uses any
other, rather than silently ignoring it:

  type, enum, const, properties, required, additionalProperties, items,
  minItems, maxItems, minLength, maxLength, minimum, maximum, pattern,
  $ref (local only: "#/$defs/<name>"), $defs, description, title, $schema

`validate(value, schema)` returns a list of problems, each "<path>: <what>",
empty when the value is valid. It never raises on bad data, only on a schema
that uses a keyword this file does not implement.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

SCHEMAS = Path(__file__).parent / "schemas"

KNOWN = {
    "type", "enum", "const", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "pattern",
    "$ref", "$defs", "description", "title", "$schema", "$id",
}

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is an int in Python; JSON keeps them apart, so does this
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}

MAX_PROBLEMS = 20   # enough to fix a request; a hostile body cannot make a huge reply


class SchemaError(ValueError):
    """The schema itself is wrong (a programming error, not bad input)."""


@cache
def load(name: str) -> dict:
    """A schema file from nam_agent/nam/schemas/, e.g. load("request")."""
    path = SCHEMAS / f"{name}.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    check_schema(schema)
    return schema


def check_schema(schema, where: str = "#") -> None:
    """Refuse any keyword this validator would not enforce."""
    if not isinstance(schema, dict):
        if isinstance(schema, bool):
            return
        raise SchemaError(f"{where}: a schema must be an object")
    unknown = set(schema) - KNOWN
    if unknown:
        raise SchemaError(f"{where}: unsupported keyword(s) {sorted(unknown)}")
    for key in ("properties", "$defs"):
        for name, sub in (schema.get(key) or {}).items():
            check_schema(sub, f"{where}/{key}/{name}")
    for key in ("items", "additionalProperties"):
        if key in schema:
            check_schema(schema[key], f"{where}/{key}")
    ref = schema.get("$ref")
    if ref is not None and not str(ref).startswith("#/$defs/"):
        raise SchemaError(f"{where}: only local '#/$defs/' refs are supported")


def validate(value, schema: dict) -> list[str]:
    problems: list[str] = []
    _check(value, schema, "$", schema, problems)
    return problems[:MAX_PROBLEMS]


def _resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"][len("#/$defs/"):]
        try:
            schema = root["$defs"][name]
        except KeyError:
            raise SchemaError(f"unknown $ref {schema['$ref']}") from None
        seen += 1
        if seen > 20:
            raise SchemaError("$ref loop")
    return schema


def _check(value, schema, path: str, root: dict, out: list[str]) -> None:
    if len(out) >= MAX_PROBLEMS:
        return
    if schema is True:
        return
    if schema is False:
        out.append(f"{path}: not allowed")
        return
    schema = _resolve(schema, root)

    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_TYPES[t](value) for t in names):
            out.append(f"{path}: expected {' or '.join(names)}, got {_kind(value)}")
            return

    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: must be one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: does not match {schema['pattern']}")

    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
            return   # do not walk a list that is already too long
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], f"{path}[{i}]", root, out)

    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                out.append(f"{path}: missing '{name}'")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            where = f"{path}.{name}"
            if name in props:
                _check(item, props[name], where, root, out)
            elif extra is False:
                out.append(f"{path}: unexpected field '{str(name)[:40]}'")
            elif isinstance(extra, dict):
                _check(item, extra, where, root, out)


def _kind(value) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPES[name](value):
            return name
    return type(value).__name__

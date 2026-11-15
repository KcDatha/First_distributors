"""A small JSON-schema checker (stdlib) for strict structured outputs: just the subset those support."""

from __future__ import annotations

_TYPES = {"string": str, "boolean": bool, "null": type(None), "array": list, "object": dict}


def _is(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    node = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def validate(value, schema: dict, _root: dict | None = None, _path: str = "$") -> list[str]:
    """Problems found (empty list = the value matches). Supports type, enum, const, properties, required,
    additionalProperties, items, anyOf, and local $ref/$defs."""
    root = _root or schema
    schema = _resolve(schema, root)
    errs: list[str] = []
    if "anyOf" in schema:
        if not any(not validate(value, s, root, _path) for s in schema["anyOf"]):
            errs.append(f"{_path}: matches none of the allowed alternatives")
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in types):
            return [f"{_path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{_path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{_path}: expected {schema['const']!r}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errs.append(f"{_path}: missing required '{name}'")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errs.append(f"{_path}: unexpected property '{name}'")
        for name, sub in props.items():
            if name in value:
                errs += validate(value[name], sub, root, f"{_path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += validate(item, schema["items"], root, f"{_path}[{i}]")
    return errs


def check_strict(schema: dict, _path: str = "$") -> None:
    """Raise ValueError when a schema is not usable for strict structured outputs (every object needs
    additionalProperties false and all its properties required), so the mistake shows up before a request."""
    if not isinstance(schema, dict):
        raise ValueError(f"{_path}: schema must be an object")
    for name, sub in schema.get("$defs", {}).items():
        check_strict(sub, f"{_path}.$defs.{name}")
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{_path}: strict schemas need additionalProperties: false on every object")
        missing = [p for p in props if p not in schema.get("required", [])]
        if missing:
            raise ValueError(f"{_path}: strict schemas need every property in 'required' (missing {missing})")
        for name, sub in props.items():
            check_strict(sub, f"{_path}.{name}")
    if "items" in schema:
        check_strict(schema["items"], f"{_path}[]")
    for i, sub in enumerate(schema.get("anyOf", [])):
        check_strict(sub, f"{_path}.anyOf[{i}]")

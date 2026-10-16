"""Tiny JSON Schema (draft-07 subset) validator: type, properties, required, items, enum,
additionalProperties:false, minItems, minLength, pattern (PLAN 2.7)."""
import json
import os
import re

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_path(name):
    return os.path.join(SCHEMA_DIR, name + ".json")


def load_schema(name):
    with open(schema_path(name)) as f:
        return json.load(f)


def validate(obj, schema, path="$"):
    """Return a list of error strings; empty means valid."""
    errs = []
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_TYPES[n](obj) for n in names):
            return ["%s: expected %s, got %s" % (path, "|".join(names), type(obj).__name__)]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append("%s: %r not in %s" % (path, obj, schema["enum"]))
    if isinstance(obj, str):
        if len(obj) < schema.get("minLength", 0):
            errs.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], obj):
            errs.append("%s: %r does not match %s" % (path, obj, schema["pattern"]))
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            errs.append("%s: fewer than minItems %d" % (path, schema["minItems"]))
        if "items" in schema:
            for i, item in enumerate(obj):
                errs += validate(item, schema["items"], "%s[%d]" % (path, i))
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                errs.append("%s: missing required %r" % (path, k))
        for k, v in obj.items():
            if k in props:
                errs += validate(v, props[k], "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                errs.append("%s: unexpected property %r" % (path, k))
    return errs

"""Small fail-closed JSON Schema subset used by the A1 prototype.

The supported keywords are intentionally explicit.  A later adapter may
replace this validator, but unsupported keywords fail rather than being
silently ignored.
"""

from __future__ import annotations

from typing import Any


class OutputSchemaError(ValueError):
    """Raised when output or schema is invalid for the supported subset."""


_SUPPORTED_KEYWORDS = {
    "$schema",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
}

_SUPPORTED_TYPES = {
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
}


def validate_schema_definition(schema: dict[str, Any], path: str = "$") -> None:
    """Validate every branch of a schema before a agent can be started."""
    if not isinstance(schema, dict) or not schema:
        raise OutputSchemaError(f"{path}: schema must be a non-empty object.")
    unsupported = set(schema) - _SUPPORTED_KEYWORDS
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise OutputSchemaError(f"{path}: unsupported schema keyword(s): {names}")

    expected_type = schema.get("type")
    types: list[str]
    if isinstance(expected_type, str):
        types = [expected_type]
    elif isinstance(expected_type, list) and expected_type:
        if not all(isinstance(item, str) for item in expected_type):
            raise OutputSchemaError(f"{path}: type list must contain strings.")
        if len(expected_type) != len(set(expected_type)):
            raise OutputSchemaError(f"{path}: type list must not contain duplicates.")
        types = expected_type
    else:
        raise OutputSchemaError(f"{path}: an explicit type is required.")
    unknown_types = set(types) - _SUPPORTED_TYPES
    if unknown_types:
        raise OutputSchemaError(
            f"{path}: unsupported schema type(s): {sorted(unknown_types)!r}."
        )

    for text_key in ("$schema", "title", "description"):
        if text_key in schema and not isinstance(schema[text_key], str):
            raise OutputSchemaError(f"{path}: {text_key} must be a string.")

    properties = schema.get("properties")
    if properties is not None:
        if "object" not in types or not isinstance(properties, dict):
            raise OutputSchemaError(
                f"{path}: properties requires object type and an object value."
            )
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, dict):
                raise OutputSchemaError(
                    f"{path}: each property must have a string name and object schema."
                )
            validate_schema_definition(child, f"{path}.properties.{name}")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise OutputSchemaError(f"{path}: required must be a string array.")
        if len(required) != len(set(required)):
            raise OutputSchemaError(f"{path}: required must not contain duplicates.")
        if not isinstance(properties, dict) or set(required) - set(properties):
            raise OutputSchemaError(
                f"{path}: required names must all exist in properties."
            )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise OutputSchemaError(f"{path}: additionalProperties must be boolean.")

    items = schema.get("items")
    if items is not None:
        if "array" not in types or not isinstance(items, dict):
            raise OutputSchemaError(
                f"{path}: items requires array type and an object schema."
            )
        validate_schema_definition(items, f"{path}.items")

    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise OutputSchemaError(f"{path}: {key} must be a non-negative integer.")
    for lower, upper in (("minItems", "maxItems"), ("minLength", "maxLength")):
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise OutputSchemaError(f"{path}: {lower} exceeds {upper}.")
    for key in ("minimum", "maximum"):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise OutputSchemaError(f"{path}: {key} must be numeric.")
    if (
        "minimum" in schema
        and "maximum" in schema
        and schema["minimum"] > schema["maximum"]
    ):
        raise OutputSchemaError(f"{path}: minimum exceeds maximum.")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise OutputSchemaError(f"{path}: enum must be a non-empty array.")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise OutputSchemaError(f"Unsupported JSON Schema type: {expected!r}")


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate *value* against the prototype's supported schema subset."""
    if path == "$":
        validate_schema_definition(schema)
    unsupported = set(schema) - _SUPPORTED_KEYWORDS
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise OutputSchemaError(f"{path}: unsupported schema keyword(s): {names}")

    if "enum" in schema and value not in schema["enum"]:
        raise OutputSchemaError(f"{path}: value is not in enum.")
    if "const" in schema and value != schema["const"]:
        raise OutputSchemaError(f"{path}: value does not match const.")

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_type(value, item) for item in expected_type):
            raise OutputSchemaError(f"{path}: value does not match any allowed type.")
    elif expected_type is not None and not _matches_type(value, expected_type):
        raise OutputSchemaError(f"{path}: expected {expected_type}.")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise OutputSchemaError(f"{path}: properties must be an object.")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise OutputSchemaError(f"{path}: required must be a string array.")
        missing = [name for name in required if name not in value]
        if missing:
            raise OutputSchemaError(f"{path}: missing required keys {missing!r}.")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise OutputSchemaError(
                    f"{path}: additional properties are forbidden: {sorted(extra)!r}."
                )
        for name, child_schema in properties.items():
            if name in value:
                if not isinstance(child_schema, dict):
                    raise OutputSchemaError(
                        f"{path}.{name}: property schema must be an object."
                    )
                validate_json_schema(value[name], child_schema, f"{path}.{name}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise OutputSchemaError(f"{path}: array is shorter than minItems.")
        if maximum is not None and len(value) > maximum:
            raise OutputSchemaError(f"{path}: array is longer than maxItems.")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise OutputSchemaError(f"{path}: items must be an object.")
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise OutputSchemaError(f"{path}: string is shorter than minLength.")
        if maximum is not None and len(value) > maximum:
            raise OutputSchemaError(f"{path}: string is longer than maxLength.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise OutputSchemaError(f"{path}: number is less than minimum.")
        if maximum is not None and value > maximum:
            raise OutputSchemaError(f"{path}: number is greater than maximum.")

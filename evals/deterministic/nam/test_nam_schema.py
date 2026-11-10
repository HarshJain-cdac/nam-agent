"""The NAM contract's JSON-schema validator (nam_agent/nam/schema.py): the keywords
it enforces, and that it refuses a schema using one it does not."""

import pytest

from nam_agent.nam import schema

OBJ = {
    "type": "object", "required": ["n"], "additionalProperties": False,
    "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 5},
        "tag": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 4},
        "kind": {"enum": ["a", "b"]},
        "list": {"type": "array", "maxItems": 2, "items": {"$ref": "#/$defs/item"}},
        "maybe": {"type": ["string", "null"]},
    },
    "$defs": {"item": {"type": "object", "required": ["id"]}},
}


def test_valid_value_has_no_problems():
    assert schema.validate({"n": 3, "tag": "ab", "kind": "a", "list": [{"id": 1}],
                            "maybe": None}, OBJ) == []


@pytest.mark.parametrize("value, fragment", [
    ({}, "missing 'n'"),
    ({"n": True}, "expected integer, got boolean"),   # JSON keeps bool and int apart
    ({"n": 9}, "above 5"),
    ({"n": 1, "tag": "AB"}, "does not match"),
    ({"n": 1, "tag": "abcde"}, "longer than 4"),
    ({"n": 1, "kind": "c"}, "must be one of"),
    ({"n": 1, "list": [{}]}, "$.list[0]: missing 'id'"),
    ({"n": 1, "list": [{"id": 1}] * 3}, "more than 2 items"),
    ({"n": 1, "extra": 1}, "unexpected field 'extra'"),
    ([], "expected object, got array"),
])
def test_each_keyword_is_enforced(value, fragment):
    problems = schema.validate(value, OBJ)
    assert any(fragment in p for p in problems), problems


def test_a_schema_with_an_unknown_keyword_is_refused():
    # silently ignoring "oneOf" would let invalid bodies through
    with pytest.raises(schema.SchemaError):
        schema.check_schema({"type": "object", "oneOf": []})
    with pytest.raises(schema.SchemaError):
        schema.check_schema({"$ref": "http://elsewhere/schema.json"})


def test_problem_list_is_capped():
    many = {"type": "array", "items": {"type": "string"}}
    assert len(schema.validate(list(range(500)), many)) == schema.MAX_PROBLEMS


@pytest.mark.parametrize("name", ["request", "response", "error", "flow.need.capture"])
def test_shipped_schemas_load(name):
    assert schema.load(name)["type"] == "object"

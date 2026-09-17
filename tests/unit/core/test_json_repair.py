import json

from core.llm_wrapper import _escape_control_chars_in_strings


def test_raw_newline_in_string_becomes_parseable():
    raw = '{"action": "tabs", "thinking": "line1\nline2", "value": "list"}'
    fixed = _escape_control_chars_in_strings(raw)
    parsed = json.loads(fixed)
    assert parsed["thinking"] == "line1\nline2"
    assert parsed["action"] == "tabs"


def test_raw_tab_and_carriage_return_repaired():
    raw = '{"msg": "a\tb\rc"}'
    parsed = json.loads(_escape_control_chars_in_strings(raw))
    assert parsed["msg"] == "a\tb\rc"


def test_already_escaped_sequences_untouched():
    raw = '{"msg": "a\\nb", "n": 1}'
    parsed = json.loads(_escape_control_chars_in_strings(raw))
    assert parsed["msg"] == "a\nb"
    assert parsed["n"] == 1


def test_structure_outside_strings_untouched():
    raw = '{\n  "a": 1,\n  "b": [2, 3]\n}'
    assert json.loads(_escape_control_chars_in_strings(raw)) == {"a": 1, "b": [2, 3]}


def test_nested_quotes_and_braces_untouched():
    raw = '{"fn": "() => \\"x\\"", "obj": {"k": "v"}}'
    parsed = json.loads(_escape_control_chars_in_strings(raw))
    assert parsed["fn"] == '() => "x"'
    assert parsed["obj"] == {"k": "v"}

import json
from copy import deepcopy

import pytest

from app.security.privacy import ToolResultPrivacy


@pytest.fixture
def result():
    return {
        "tool": "read_clipboard",
        "call_id": "call_123",
        "success": False,
        "status": "failed",
        "arguments": {"path": "/Users/private/.env"},
        "error": "password=secret",
        "result": {"nested": ["secret"], "truncated": True},
    }


@pytest.mark.parametrize("provider", ["openai", "unknown", "OLLAMA", ""])
def test_remote_default_excludes_all_payload_fields(provider, result):
    assert ToolResultPrivacy(provider).filter_result(result) == {
        "tool": "read_clipboard",
        "call_id": "call_123",
        "success": False,
        "status": "failed",
        "result_withheld": True,
    }


@pytest.mark.parametrize(
    "policy",
    [
        ToolResultPrivacy("ollama"),
        ToolResultPrivacy("openai", "all"),
        ToolResultPrivacy("openai", "allowlist", {"read_clipboard"}),
    ],
)
def test_full_disclosure_is_explicit_and_detached(policy, result):
    copy = policy.filter_result(result)
    assert copy == result
    copy["result"]["nested"].append("changed")
    assert result["result"]["nested"] == ["secret"]


def test_allowlist_is_exact_and_immutable(result):
    allowed = {"other_tool"}
    policy = ToolResultPrivacy("openai", "allowlist", allowed)
    allowed.add("read_clipboard")
    assert policy.filter_result(result)["result_withheld"]


def test_malformed_metadata_fails_closed():
    policy = ToolResultPrivacy("openai")
    for malformed in [None, [], "secret"]:
        assert policy.filter_result(malformed) == {
            "success": False,
            "status": "failed",
            "result_withheld": True,
        }
    assert policy.filter_result(
        {
            "tool": {"secret": "secret"},
            "call_id": "/private/secret",
            "status": ["secret"],
            "success": "secret",
            "error": "secret",
        }
    ) == {"success": False, "status": "failed", "result_withheld": True}


def test_history_filters_outputs_and_summaries_without_mutation(result):
    history = [
        {"type": "function_call_output", "call_id": "call_123", "output": json.dumps(result)},
        {"role": "assistant", "content": json.dumps([result])},
        {"role": "user", "content": "My secret user-authored prompt"},
        {"role": "assistant", "content": "Ordinary assistant prose"},
    ]
    original = deepcopy(history)
    policy = ToolResultPrivacy("openai")
    filtered = policy.filter_history(history)
    assert "secret" not in filtered[0]["output"]
    assert "secret" not in filtered[1]["content"]
    assert filtered[2:] == history[2:]
    assert json.loads(policy.summary([result])) == [policy.filter_result(result)]
    assert history == original


@pytest.mark.parametrize("output", ["not valid JSON secret", "[]", "null", "123", None])
def test_history_malformed_output_withheld(output):
    filtered = ToolResultPrivacy("openai").filter_history(
        [
            {"type": "function_call_output", "output": output},
        ]
    )
    assert json.loads(filtered[0]["output"])["result_withheld"]


def test_local_history_preserves_results(result):
    history = [{"type": "function_call_output", "output": json.dumps(result)}]
    assert ToolResultPrivacy("ollama").filter_history(history) == history


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="privacy mode"):
        ToolResultPrivacy("openai", "typo")

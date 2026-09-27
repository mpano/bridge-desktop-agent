import json
from pathlib import Path
from unittest.mock import patch

from app.config.settings import Settings
from app.diagnostics import collect_diagnostics


def config(tmp_path: Path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "agent.db",
        openai_api_key="private-key-value",
        api_token="private-token-value",
        **kwargs,
    )


def test_report_omits_secrets_and_paths_and_does_not_create_database(tmp_path):
    settings = config(tmp_path)
    report = collect_diagnostics(settings)
    serialized = json.dumps(report)
    assert "private-key-value" not in serialized
    assert "private-token-value" not in serialized
    assert str(tmp_path) not in serialized
    assert not settings.database_path.exists()
    assert report["provider"] == {
        "name": "openai",
        "model": settings.openai_model,
        "remote_tool_results": "status_only",
        "remote_tool_result_allowlist": [],
    }
    assert (
        next(c for c in report["checks"] if c["name"] == "macos_permissions")["status"] == "warning"
    )


def test_missing_configuration_and_non_macos(tmp_path):
    settings = Settings(
        _env_file=None,
        openai_api_key="",
        api_token="",
        database_path=tmp_path / "missing" / "agent.db",
    )
    with patch("app.diagnostics.platform.system", return_value="Linux"):
        report = collect_diagnostics(settings)
    checks = {check["name"]: check for check in report["checks"]}
    assert report["ready"] is False
    for name in ("macos", "openai_api_key", "api_token", "database_directory"):
        assert checks[name]["status"] == "warning"
    assert not settings.database_path.parent.exists()


def test_available_prerequisites_without_executing_tools(tmp_path):
    with (
        patch("app.diagnostics.platform.system", return_value="Darwin"),
        patch("app.diagnostics.Path.is_file", return_value=True),
        patch("app.diagnostics.os.access", return_value=True),
        patch("subprocess.run", side_effect=AssertionError("must not execute commands")),
    ):
        report = collect_diagnostics(config(tmp_path))
    assert report["ready"] is False
    assert all(
        check["status"] == "ok"
        for check in report["checks"]
        if check["name"] != "macos_permissions"
    )


def test_filesystem_probe_errors_are_safe(tmp_path):
    with patch("app.diagnostics.os.access", side_effect=PermissionError("secret path")):
        report = collect_diagnostics(config(tmp_path))
    assert report["ready"] is False
    assert "secret path" not in json.dumps(report)


def test_local_provider_needs_no_openai_key_and_omits_endpoint(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "agent.db",
        llm_provider="ollama",
        openai_api_key="",
        local_llm_model="qwen3:8b",
        local_llm_base_url="http://127.0.0.1:11434",
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["get_volume"],
    )
    with patch("socket.create_connection", side_effect=AssertionError("no network probes")):
        report = collect_diagnostics(settings)
    assert not any(check["name"] == "openai_api_key" for check in report["checks"])
    assert any(check["name"] == "local_llm" for check in report["checks"])
    assert report["ready"] is False
    assert report["provider"] == {
        "name": "ollama",
        "model": "qwen3:8b",
        "remote_tool_results": "allowlist",
        "remote_tool_result_allowlist": ["get_volume"],
    }
    assert "127.0.0.1" not in json.dumps(report)

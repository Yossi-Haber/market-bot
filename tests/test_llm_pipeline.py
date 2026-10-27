"""Integration tests for the optional narrative pipeline using only MockProvider."""

import json
from pathlib import Path

import pytest

from llm.narrative import run_narrative
from llm.providers import MockProvider, ProviderError


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def setup_output(monkeypatch, tmp_path):
    import llm.narrative as narrative

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    (tmp_path / "daily_report_data.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "daily_report.md").write_text("# דוח דטרמיניסטי\n", encoding="utf-8")
    monkeypatch.setattr(narrative, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(narrative, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(narrative, "load_dotenv", lambda *args, **kwargs: None)
    return narrative


def selection(path):
    return json.loads((path / "daily_report_selection.json").read_text(encoding="utf-8"))


def test_llm_disabled_uses_deterministic_output(monkeypatch, tmp_path):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "false")
    provider = MockProvider("valid")
    result = run_narrative(provider=provider)
    assert result == tmp_path / "daily_report_final.md"
    assert provider.calls == 0
    assert selection(tmp_path)["selected_source"] == "DETERMINISTIC"
    assert selection(tmp_path)["selection_reason"] == "LLM_DISABLED"
    assert selection(tmp_path)["llm_attempted"] is False
    assert selection(tmp_path)["guardrails_checked"] is False
    assert (tmp_path / "daily_report_final.md").read_text(encoding="utf-8") == "# דוח דטרמיניסטי\n"


def test_valid_mock_is_selected_as_llm(monkeypatch, tmp_path):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "true")
    provider = MockProvider("valid")
    run_narrative(provider=provider)
    assert provider.calls == 1
    assert selection(tmp_path)["selected_source"] == "LLM"
    assert (tmp_path / "daily_report_llm.md").exists()
    assert (tmp_path / "daily_report_final.md").read_text(encoding="utf-8").startswith("# סיכום")


def test_enabled_without_api_key_uses_fallback(monkeypatch, tmp_path):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    run_narrative()
    result = selection(tmp_path)
    assert result["selected_source"] == "DETERMINISTIC"
    assert result["errors"] == ["LLM_PROVIDER_UNAVAILABLE"]
    assert result["llm_model"] == "gpt-5.6-luna"


def test_model_not_found_uses_sanitized_fallback(monkeypatch, tmp_path):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "true")

    class ModelNotFoundProvider:
        def generate_narrative(self, payload):
            raise ProviderError("OPENAI_MODEL_NOT_FOUND")

    run_narrative(provider=ModelNotFoundProvider())
    result = selection(tmp_path)
    assert result["selected_source"] == "DETERMINISTIC"
    assert result["errors"] == ["OPENAI_MODEL_NOT_FOUND"]
    assert result["llm_model"] == "gpt-5.6-luna"


@pytest.mark.parametrize("mode", ["numeric", "causal", "ticker", "lagging"])
def test_unsafe_mock_narrative_falls_back(monkeypatch, tmp_path, mode):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "true")
    run_narrative(provider=MockProvider(mode))
    result = selection(tmp_path)
    assert result["selected_source"] == "DETERMINISTIC"
    assert result["fallback_used"] is True
    assert (tmp_path / "debug" / "rejected_llm_output.md").exists()
    assert (tmp_path / "daily_report_final.md").read_text(encoding="utf-8") == "# דוח דטרמיניסטי\n"


@pytest.mark.parametrize("mode", ["timeout", "empty"])
def test_provider_failure_falls_back_without_llm_report(monkeypatch, tmp_path, mode):
    setup_output(monkeypatch, tmp_path)
    monkeypatch.setenv("ENABLE_LLM", "true")
    run_narrative(provider=MockProvider(mode))
    result = selection(tmp_path)
    assert result["selected_source"] == "DETERMINISTIC"
    assert result["fallback_used"] is True
    assert not (tmp_path / "daily_report_llm.md").exists()


def test_not_ready_makes_no_provider_call(monkeypatch, tmp_path):
    setup_output(monkeypatch, tmp_path)
    data = json.loads((tmp_path / "daily_report_data.json").read_text(encoding="utf-8"))
    data["publication_readiness"]["status"] = "NOT_READY"
    (tmp_path / "daily_report_data.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("ENABLE_LLM", "true")
    provider = MockProvider("valid")
    assert run_narrative(provider=provider) is None
    assert provider.calls == 0
    assert selection(tmp_path)["errors"] == ["REPORT_NOT_READY"]
    assert not (tmp_path / "daily_report_llm.md").exists()

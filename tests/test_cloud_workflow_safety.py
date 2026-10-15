from pathlib import Path


def test_phase2b_workflow_is_manual_read_only_and_has_no_production_secrets():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-persistence-validation.yml"
    content = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "contents: read" in content
    assert "contents: write" not in content
    assert "timeout-minutes:" in content
    assert "MARKETBOT_CLOUD_LIVE_ENABLED: \"0\"" in content
    assert "MARKETBOT_CLOUD_TELEGRAM_ENABLED: \"0\"" in content
    assert "--execute" not in content
    assert "retention-days: 3" in content

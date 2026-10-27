from pathlib import Path


def test_artifact_upload_smoke_is_manual_fixture_only_and_uses_production_validator():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/phase4a-cloud-validation.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "- artifact_upload_smoke" in workflow
    assert "if: inputs.validation_mode == 'artifact_upload_smoke'" in workflow
    assert "python validate_production_report_artifacts.py" in workflow
    assert "production_state_readonly" not in workflow.split("      - name: Validate fixture using production artifact rules", 1)[1].split("      - name:", 1)[0]
    assert "MARKETBOT_CLOUD_LIVE_ENABLED: \"0\"" in workflow
    assert "MARKETBOT_CLOUD_TELEGRAM_ENABLED: \"0\"" in workflow
    assert "args=(--execute" not in workflow
    assert "uses: actions/upload-artifact@v7" in workflow
    assert "name: marketbot-artifact-validation-smoke-${{ github.run_id }}" in workflow
    assert "retention-days: 3" in workflow

    upload = workflow.split("      - name: Upload clearly labeled artifact validation fixture\n", 1)[1]
    upload = upload.split("      - name:", 1)[0]
    expected_files = (
        "daily_report_data.json",
        "daily_report_selection.json",
        "daily_report_final.md",
        "daily_report_card_v3.html",
        "daily_report_card_v3.png",
        "daily_report_v3_visual_validation.json",
    )
    for name in expected_files:
        assert f"/fixture/{name}" in upload
    assert "output/**" not in upload
    assert "logs/" not in upload
    assert "cache/" not in upload


def test_artifact_validator_scans_only_named_secret_environment_values():
    from validate_production_report_artifacts import SECRET_ENV_NAMES

    assert "MARKETAUX_API_TOKEN" in SECRET_ENV_NAMES
    assert "TELEGRAM_BOT_TOKEN" in SECRET_ENV_NAMES
    assert "MARKETBOT_D1_STATE_HMAC_SECRET" in SECRET_ENV_NAMES
    assert "TELEGRAM_RECIPIENT_ID_HMAC_KEY" in SECRET_ENV_NAMES

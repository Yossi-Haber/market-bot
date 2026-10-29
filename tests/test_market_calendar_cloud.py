from datetime import date, datetime, timezone
import json

import pytest

from market_calendar import MarketCalendarError, get_session, is_completed_session, resolve_market_date


@pytest.fixture(autouse=True)
def isolate_cloud_test_configuration(monkeypatch):
    """Git-state fixtures must not load local production credentials or backend."""
    monkeypatch.setattr("run_daily_cloud.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("MARKETBOT_STATE_BACKEND", "github")


def test_weekends_regular_and_exceptional_closures_are_not_sessions():
    for day in ("2026-10-17", "2026-07-03", "2001-09-11", "2018-12-05"):
        assert get_session(day) is None
    assert get_session("2026-10-16") is not None


def test_nyse_early_close_is_used_for_date_eligibility():
    session = get_session("2026-11-27")
    assert session is not None
    assert session.closes_at.hour == 13 and session.closes_at.minute == 0
    assert is_completed_session("2026-11-27", now=datetime(2026, 11, 27, 13, 1, tzinfo=session.closes_at.tzinfo))
    assert not is_completed_session("2026-11-27", now=datetime(2026, 11, 27, 12, 59, tzinfo=session.closes_at.tzinfo))


def test_eastern_dst_changes_utc_close_not_local_close():
    before = get_session("2026-03-06")
    after = get_session("2026-03-09")
    assert before and after
    assert before.closes_at.hour == after.closes_at.hour == 16
    assert before.closes_at.astimezone(timezone.utc).hour == 21
    assert after.closes_at.astimezone(timezone.utc).hour == 20


def test_delayed_start_after_weekend_resolves_friday_once_prior_days_complete():
    completed = {"2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"}
    now = datetime(2026, 10, 17, 12, 0, tzinfo=get_session("2026-10-16").closes_at.tzinfo)
    assert resolve_market_date(now=now, completed_dates=completed, start_date="2026-10-12") == "2026-10-16"


def test_manual_date_requires_completed_session_and_honors_state_genesis():
    with pytest.raises(MarketCalendarError, match="COMPLETED_NYSE_SESSION"):
        resolve_market_date(now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc), explicit_date="2026-10-14")
    with pytest.raises(MarketCalendarError, match="PRECEDES_STATE_GENESIS"):
        resolve_market_date(now=datetime(2026, 10, 14, 23, tzinfo=timezone.utc), explicit_date="2026-10-13", start_date="2026-10-14")


def test_missed_sessions_require_explicit_operator_review():
    now = datetime(2026, 10, 14, 22, tzinfo=timezone.utc)
    with pytest.raises(MarketCalendarError, match="MISSED_MARKET_SESSION_REQUIRES_REVIEW"):
        resolve_market_date(now=now, start_date="2026-10-12")


def test_cloud_adapter_calendar_check_is_offline_and_emits_manifest(tmp_path, monkeypatch):
    import run_daily_cloud

    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--calendar-check", "--market-date", "2026-10-13"], now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert result == 0
    data = (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert '"provider_calls": 0' in data
    assert '"telegram_sends": 0' in data


def test_cloud_execute_refuses_until_windows_and_quota_ownership_are_exclusive(state_repos, tmp_path, monkeypatch):
    import run_daily_cloud

    _, repo, _ = state_repos
    from persistence.github_state import GitHubStateStore
    GitHubStateStore(repo).update(lambda state: state.update({"created_at": "2026-10-13T00:00:00+00:00"}))
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--execute", "--market-date", "2026-10-14"], now=datetime(2026, 10, 14, 21, tzinfo=timezone.utc))
    assert result == 1
    assert "CLOUD_OWNERSHIP_NOT_EXCLUSIVE" in (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert GitHubStateStore(repo).read(fetch=True)["runs"] == {}


def test_cloud_telegram_execute_refuses_before_claim_when_delivery_gate_is_closed(state_repos, tmp_path, monkeypatch):
    import run_daily_cloud
    from persistence.github_state import GitHubStateStore

    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    store.update(lambda state: (state.update({"created_at": "2026-10-13T00:00:00+00:00"}), state["ownership"].update({"cloud_quota_exclusive": True, "windows_sender_disabled": True, "cloud_delivery_enabled": False})))
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setenv("MARKETBOT_CLOUD_TELEGRAM_ENABLED", "1")
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)

    result = run_daily_cloud.run(["--execute", "--send-telegram", "--market-date", "2026-10-14"], now=datetime(2026, 10, 14, 21, tzinfo=timezone.utc))

    assert result == 1
    assert "CLOUD_DELIVERY_OWNERSHIP_NOT_EXCLUSIVE" in (tmp_path / "latest.json").read_text(encoding="utf-8")
    assert store.read(fetch=True)["runs"] == {}


def test_cloud_scheduled_no_new_session_is_clean_noop(state_repos, tmp_path, monkeypatch, capsys):
    import run_daily_cloud
    from persistence.github_state import GitHubStateStore

    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    completed = {day: {"status": "COMPLETE", "run_id": (str(index) * 32)} for index, day in enumerate(("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"), start=1)}
    store.update(lambda state: state.update({"created_at": "2026-10-13T00:00:00+00:00", "runs": completed}))
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)

    # Weekend: the latest completed session has already been processed.
    result = run_daily_cloud.run(["--dry-run"], now=datetime(2026, 10, 17, 16, tzinfo=timezone.utc))

    assert result == 0
    assert "CLOUD_NOOP" in capsys.readouterr().out
    assert '"status": "NO_NEW_TRADING_SESSION"' in (tmp_path / "latest.json").read_text(encoding="utf-8")


def test_stale_cloud_send_requires_operator_review_without_automatic_recovery(state_repos, tmp_path, monkeypatch):
    import run_daily_cloud
    from persistence.github_state import GitHubStateStore

    _, repo, _ = state_repos
    store = GitHubStateStore(repo)
    started = "2026-10-14T15:20:00+00:00"
    slot = "hmac256:" + "a" * 64

    def seed(state):
        state["created_at"] = "2026-10-13T00:00:00+00:00"
        state["ownership"].update({"cloud_quota_exclusive": True, "windows_sender_disabled": True})
        state["runs"]["2026-10-14"] = {"status": "RUNNING", "run_id": "b" * 32, "started_at": started}
        state["deliveries"]["2026-10-14"] = {"recipients": {slot: {"status": "SENDING", "attempt": 1}}}

    store.update(seed)
    monkeypatch.setenv("MARKETBOT_STATE_REPO", str(repo))
    monkeypatch.setenv("MARKETBOT_CLOUD_LIVE_ENABLED", "1")
    monkeypatch.setattr(run_daily_cloud, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(run_daily_cloud, "OUTPUT_DIR", tmp_path)
    result = run_daily_cloud.run(["--execute", "--market-date", "2026-10-14"], now=datetime(2026, 10, 14, 21, tzinfo=timezone.utc))
    assert result == 1
    recovered = GitHubStateStore(repo).read(fetch=True)
    assert recovered["runs"]["2026-10-14"]["status"] == "RUNNING"
    assert recovered["deliveries"]["2026-10-14"]["recipients"][slot]["status"] == "SENDING"
    manifest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert manifest["reason"] == "INTERRUPTED_RUN_REQUIRES_OPERATOR_REVIEW"
    assert manifest["stale"] is True
    assert manifest["automatic_recovery"] is False


def test_production_workflow_is_manual_gated_and_documents_ny_schedule():
    from pathlib import Path

    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/marketbot-production.yml"
    content = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in content
    assert "confirm_production_run:" in content
    assert "send_telegram:" in content
    assert "default: false" in content
    assert "\non:\n  schedule:" not in content
    assert 'cron "15 17 * * 1-5" in timezone "America/New_York"' in content
    assert "cancel-in-progress: false" in content
    assert "timeout-minutes: 120" in content
    assert "permissions:\n  contents: read" in content
    assert "run_daily_cloud.py \"${args[@]}\"" in content
    assert "args=(--execute)" in content
    assert "args=(--execute --send-telegram)" in content
    assert content.count('args+=(--market-date "$MARKET_DATE")') == 2
    assert 'MARKET_DATE_INPUT: ${{ inputs.market_date }}' in content
    assert 'MARKET_DATE: ${{ steps.market-date.outputs.value }}' in content
    assert 're.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)' in content
    assert "datetime.date.fromisoformat(value)" in content
    assert "parsed.isoformat() != value" in content
    assert content.count("${{ inputs.market_date }}") == 1
    assert "github.ref == 'refs/heads/main'" in content
    assert "vars.MARKETBOT_CLOUD_SCHEDULE_ENABLED == '1'" in content
    assert "vars.MARKETBOT_CLOUD_LIVE_ENABLED || '0'" in content
    assert "vars.MARKETBOT_CLOUD_TELEGRAM_ENABLED || '0'" in content
    assert "MARKETBOT_D1_STATE_ADMIN_HMAC_SECRET" not in content
    assert content.count("uses: actions/upload-artifact@v7") == 1
    assert "id: report_no_telegram" in content
    assert "id: report_telegram" in content
    assert "id: report_artifact_check" in content
    assert "python validate_production_report_artifacts.py" in content
    validator = (Path(__file__).resolve().parents[1] / "validate_production_report_artifacts.py").read_text(encoding="utf-8")
    for diagnostic in (
        "REPORT_ARTIFACT_INCOMPLETE",
        "REPORT_ARTIFACT_INVALID_JSON",
        "REPORT_ARTIFACT_SECRET_DETECTED",
        "REPORT_ARTIFACT_MARKET_DATE_UNAVAILABLE",
        "REPORT_ARTIFACT_MARKET_DATE_MISMATCH",
        "REPORT_ARTIFACT_INVALID_JSON_ROOT",
        "configured_secrets_checked={checked}",
    ):
        assert diagnostic in validator
    assert (
        "if: ${{ success() && steps.report_artifact_check.outcome == 'success' "
        "&& (steps.report_no_telegram.outcome == 'success' "
        "|| steps.report_telegram.outcome == 'success') "
        "&& hashFiles('output/daily_report_data.json') != '' }}"
    ) in content
    assert "hashFiles('output/daily_report_data.json') != ''" in content
    assert "if-no-files-found: error" in content
    assert "retention-days: 3" in content
    artifact_step = content.split("      - name: Upload successful production report artifacts\n", 1)[1]
    artifact_step = artifact_step.split("      - name:", 1)[0]
    artifact_paths = [
        line.strip()
        for line in artifact_step.splitlines()
        if line.strip().startswith("output/")
    ]
    assert artifact_paths == [
        "output/daily_report_data.json",
        "output/daily_report_selection.json",
        "output/daily_report_final.md",
        "output/daily_report_card_v3.html",
        "output/daily_report_card_v3.png",
        "output/daily_report_v3_visual_validation.json",
    ]
    assert "output/**" not in artifact_step
    assert "logs/" not in artifact_step
    assert "cache/" not in artifact_step
    for secret_name in (
        "TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY",
        "MARKETBOT_D1_STATE_HMAC_SECRET", "OPENAI_API_KEY", "MARKETAUX_API_TOKEN",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS", "TELEGRAM_RECIPIENT_ID_HMAC_KEY",
    ):
        assert f"secrets.{secret_name}" in content


def test_production_artifact_guard_rejects_missing_or_secret_bearing_files(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "validate_production_report_artifacts.py"

    output = tmp_path / "output"
    (output / "cloud_operations").mkdir(parents=True)
    (output / "cloud_operations" / "latest.json").write_text(
        '{"status":"COMPLETE","market_date":"2026-10-26"}', encoding="utf-8"
    )
    names = (
        "daily_report_data.json",
        "daily_report_selection.json",
        "daily_report_final.md",
        "daily_report_card_v3.html",
        "daily_report_card_v3.png",
        "daily_report_v3_visual_validation.json",
    )
    for name in names:
        payload = b'{"market_date":"2026-10-26"}' if name == names[0] else b"{}" if name.endswith(".json") else b"safe report"
        (output / name).write_bytes(payload)

    env = os.environ.copy()
    env["TELEGRAM_BOT_TOKEN"] = "SYNTHETIC_TEST_SECRET_NEVER_LOG"
    args = [
        sys.executable,
        str(script),
        "--output-dir", str(output),
        "--manifest", str(output / "cloud_operations" / "latest.json"),
        "--requested-market-date", "2026-10-26",
    ]
    result = subprocess.run(
        args,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "REPORT_ARTIFACT_SAFETY=PASS files=6" in result.stdout
    assert "SYNTHETIC_TEST_SECRET_NEVER_LOG" not in result.stdout + result.stderr

    (output / "daily_report_final.md").write_text(
        "accidental secret SYNTHETIC_TEST_SECRET_NEVER_LOG", encoding="utf-8"
    )
    result = subprocess.run(
        args,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "REPORT_ARTIFACT_SECRET_DETECTED" in result.stdout
    assert "SYNTHETIC_TEST_SECRET_NEVER_LOG" not in result.stdout + result.stderr

    (output / "daily_report_final.md").write_text("safe report", encoding="utf-8")
    (output / names[-1]).unlink()
    result = subprocess.run(
        args,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "REPORT_ARTIFACT_INCOMPLETE" in result.stdout

    (output / names[-1]).write_text("{}", encoding="utf-8")
    (output / names[0]).write_text('{"market_date":"2026-10-27"}', encoding="utf-8")
    result = subprocess.run(
        args,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "REPORT_ARTIFACT_MARKET_DATE_MISMATCH" in result.stdout


@pytest.mark.parametrize(
    ("value", "expected_exit", "expected_output"),
    [
        ("", 0, "value=\n"),
        ("2026-10-26", 0, "value=2026-10-26\n"),
        ("2026-2-06", 1, ""),
        ("2026-02-30", 1, ""),
        ("2026-10-26; echo unsafe", 1, ""),
    ],
)
def test_workflow_market_date_validator_executes_strictly(value, expected_exit, expected_output, tmp_path):
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/marketbot-production.yml"
    content = workflow.read_text(encoding="utf-8")
    step = content.split("      - name: Validate optional manual market date\n", 1)[1].split("      - name:", 1)[0]
    script = step.split("          python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    script = textwrap.dedent(script)
    output_path = tmp_path / "github_output.txt"
    env = os.environ.copy()
    env.update({"MARKET_DATE_INPUT": value, "GITHUB_OUTPUT": str(output_path)})

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_exit
    actual_output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    assert actual_output == expected_output

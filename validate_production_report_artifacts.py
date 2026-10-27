"""Validate the six allowlisted report artifacts before a GitHub upload."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path


ARTIFACT_NAMES = (
    "daily_report_data.json",
    "daily_report_selection.json",
    "daily_report_final.md",
    "daily_report_card_v3.html",
    "daily_report_card_v3.png",
    "daily_report_v3_visual_validation.json",
)
JSON_ARTIFACT_NAMES = {ARTIFACT_NAMES[0], ARTIFACT_NAMES[1], ARTIFACT_NAMES[5]}
SECRET_ENV_NAMES = (
    "TWELVE_DATA_API_KEY",
    "FINNHUB_API_KEY",
    "FRED_API_KEY",
    "OPENAI_API_KEY",
    "MARKETAUX_API_TOKEN",
    "MARKETBOT_D1_STATE_HMAC_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_IDS",
    "TELEGRAM_RECIPIENT_ID_HMAC_KEY",
)


class ArtifactValidationError(ValueError):
    """Stable, safe error code for artifact validation failures."""


def validate_report_artifacts(
    output_dir: Path,
    manifest_path: Path,
    requested_market_date: str = "",
    environ: dict[str, str] | None = None,
) -> int:
    """Validate files, JSON, report date and configured-secret non-disclosure.

    Returns the number of configured non-empty secret values checked. Values
    are never included in diagnostics.
    """
    environ = os.environ if environ is None else environ
    output_dir = output_dir.resolve()
    manifest_path = manifest_path.resolve()
    configured_secrets = {
        name: environ[name].encode("utf-8")
        for name in SECRET_ENV_NAMES
        if environ.get(name)
    }
    parsed_json: dict[str, object] = {}

    for name in ARTIFACT_NAMES:
        path = output_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactValidationError(f"REPORT_ARTIFACT_INCOMPLETE: {name}")
        content = path.read_bytes()
        if name in JSON_ARTIFACT_NAMES:
            try:
                parsed_json[name] = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactValidationError(f"REPORT_ARTIFACT_INVALID_JSON: {name}") from exc
        for env_name, secret in configured_secrets.items():
            if secret in content:
                raise ArtifactValidationError(
                    f"REPORT_ARTIFACT_SECRET_DETECTED: file={name} variable={env_name}"
                )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        resolved_date = str(manifest["market_date"])
        if date.fromisoformat(resolved_date).isoformat() != resolved_date:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("REPORT_ARTIFACT_MARKET_DATE_UNAVAILABLE") from exc

    report = parsed_json[ARTIFACT_NAMES[0]]
    if not isinstance(report, dict):
        raise ArtifactValidationError("REPORT_ARTIFACT_INVALID_JSON_ROOT")
    report_date = report.get("market_date")
    if report_date != resolved_date or (
        requested_market_date and requested_market_date != resolved_date
    ):
        raise ArtifactValidationError("REPORT_ARTIFACT_MARKET_DATE_MISMATCH")
    return len(configured_secrets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("output/cloud_operations/latest.json")
    )
    parser.add_argument("--requested-market-date", default="")
    args = parser.parse_args()
    try:
        checked = validate_report_artifacts(
            args.output_dir,
            args.manifest,
            requested_market_date=args.requested_market_date,
        )
    except ArtifactValidationError as exc:
        print(str(exc))
        return 1
    print(f"REPORT_ARTIFACT_SAFETY=PASS files={len(ARTIFACT_NAMES)} configured_secrets_checked={checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

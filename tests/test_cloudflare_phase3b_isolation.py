"""Static safeguards for the disposable Phase 3B Worker configuration."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "cloudflare" / "marketbot-state-worker"


def _config(name: str) -> dict:
    return json.loads((WORKER_DIR / name).read_text(encoding="utf-8"))


def test_test_worker_uses_same_handler_and_migrations_but_separate_database() -> None:
    production = _config("wrangler.jsonc")
    isolated = _config("wrangler.test.jsonc")

    assert isolated["name"] == "marketbot-state-api-test"
    assert isolated["workers_dev"] is True
    assert isolated["main"] == production["main"] == "src/index.ts"
    assert isolated["d1_databases"][0]["binding"] == production["d1_databases"][0]["binding"] == "DB"
    assert isolated["d1_databases"][0]["migrations_dir"] == production["d1_databases"][0]["migrations_dir"] == "migrations"
    assert isolated["d1_databases"][0]["database_name"] == "marketbot-test"
    assert isolated["d1_databases"][0]["database_id"] != production["d1_databases"][0]["database_id"]
    assert isolated["d1_databases"][0]["database_id"] == "8049a22c-eb1f-4e9d-928d-29b35dcc6f70"
    assert isolated["secrets"]["required"] == ["STATE_API_HMAC_SECRET", "STATE_ADMIN_HMAC_SECRET"]
    assert isolated["observability"]["enabled"] is False


def test_test_config_never_names_production_worker_or_database() -> None:
    text = (WORKER_DIR / "wrangler.test.jsonc").read_text(encoding="utf-8").lower()
    production = _config("wrangler.jsonc")
    prod_db_id = production["d1_databases"][0]["database_id"].lower()

    assert "marketbot-state-api\"" not in text
    assert "marketbot-state\"" not in text
    assert prod_db_id not in text
    assert "yossihabar1994.workers.dev" not in text


def test_phase3b_config_does_not_redefine_or_copy_worker_logic() -> None:
    isolated = _config("wrangler.test.jsonc")
    assert isolated["main"] == "src/index.ts"
    assert list((WORKER_DIR / "migrations").glob("*.sql"))
    assert not (WORKER_DIR / "src" / "test_index.ts").exists()

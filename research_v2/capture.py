"""Opt-in bounded capture and cache-only historical import. No provider clients."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from news.evidence_capture import _observer
from .evidence import build_packet, recorded_items, normalized_url
from .contracts import validate_packet
from .session import public_json


def atomic_json(path, value):
    serialized = public_json(value)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".v2-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(.05 * (2 ** attempt))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EvidenceCapture:
    def __init__(self, market_date, output, *, max_items=100, synthetic=False):
        self.market_date, self.root, self.max_items = market_date, Path(output), max_items
        if type(max_items) is not int or max_items < 1:
            raise ValueError("CAPTURE_BUDGET_INVALID")
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError("CAPTURE_OUTPUT_MUST_BE_NEW")
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = {"origin": "ARCHIVED_PROVIDER_RESPONSES", "market_date": market_date,
                         "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                         "synthetic": synthetic, "recordings": [], "source_files": [],
                         "capture": {"errors": 0, "budget_excluded": 0, "duplicates": 0, "retained": 0,
                                     "upstream_query_policy_unchanged": True, "complete_historical_retrieval": False}}
        self.seen, self.token = set(), None

    def __enter__(self):
        if self.token is not None:
            raise ValueError("CAPTURE_ALREADY_ACTIVE")
        self.token = _observer.set(self.record)
        try:
            self.flush()
        except Exception:
            _observer.reset(self.token)
            self.token = None
            raise
        return self

    def __exit__(self, *args):
        if self.token is not None:
            _observer.reset(self.token)
        self.token = None
        self.flush()

    def flush(self):
        atomic_json(self.root / "v2_captured_recordings.json", self.manifest)

    def record(self, provider, payload, *, scope="UNSPECIFIED", symbols=(), state="AVAILABLE", limitations=()):
        try:
            return self._record(provider, payload, scope=scope, symbols=symbols, state=state, limitations=limitations)
        except Exception:
            self.manifest["capture"]["observer_failed"] = True
            raise

    def _record(self, provider, payload, *, scope, symbols, state, limitations):
        retained, errors, omissions, rejection_reasons = [], 0, 0, {}
        original_count = 0
        try:
            for item in recorded_items(provider, payload):
                original_count += 1
                try:
                    if not item["symbols"] and provider == "finnhub":
                        item["symbols"] = list(symbols)
                        item["provider_limitations"].append("QUERY_SYMBOL_ASSOCIATION_NOT_VERIFIED_SUBJECT")
                    key = (normalized_url(item["original_url"]), item["published_at"], item["title"])
                    if key in self.seen:
                        self.manifest["capture"]["duplicates"] += 1
                        continue
                    # Validate mechanical structure/window; semantic heuristics are never applied.
                    row = {"title": item["title"], "source": item["publisher"], "url": item["original_url"],
                           "published_at": item["published_at"], "publication_timestamp_original": item["publication_timestamp_original"],
                           "retrieved_at": item["retrieved_at"], "affected_symbols": item["symbols"], "text": item["source_text"],
                           "content_completeness": item["content_completeness"], "source_category": item["source_category"],
                           "provider_limitations": item["provider_limitations"], **item["filing_metadata"]}
                    if item.get("source_provenance"):
                        row["source_provenance"] = item["source_provenance"]
                    trial = build_packet(self.market_date, [{"provider": provider, "coverage_state": "AVAILABLE", "payload": {"data": [row]} if provider == "marketaux" else [row]}])
                    if not trial["evidence"]:
                        errors += 1
                        for reason, count in trial["diagnostics"]["hygiene_rejections"].items():
                            safe_reason = reason if reason in {"OUTSIDE_WINDOW", "SOURCE_IDENTITY_MISSING", "TIMESTAMP_TIMEZONE_REQUIRED", "INVALID_SOURCE_URL", "CREDENTIAL_URL_REJECTED"} else "INVALID_SOURCE_TIMESTAMP_OR_STRUCTURE"
                            rejection_reasons[safe_reason] = rejection_reasons.get(safe_reason, 0) + count
                        continue
                    validate_packet(trial)
                    if self.manifest["capture"]["retained"] >= self.max_items:
                        omissions += 1
                        continue
                    self.seen.add(key)
                    retained.append(row)
                    self.manifest["capture"]["retained"] += 1
                except (ValueError, TypeError, KeyError):
                    errors += 1
                    rejection_reasons["INVALID_SOURCE_TIMESTAMP_OR_STRUCTURE"] = rejection_reasons.get("INVALID_SOURCE_TIMESTAMP_OR_STRUCTURE", 0) + 1
        except (ValueError, TypeError, KeyError):
            errors += 1
        self.manifest["capture"]["errors"] += errors
        self.manifest["capture"]["budget_excluded"] += omissions
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        result_limit = ["PROVIDER_RESULT_WINDOW_MAY_BE_TRUNCATED"] if isinstance(meta, dict) and meta.get("found", 0) > meta.get("returned", 0) else []
        self.manifest["recordings"].append({"provider": provider,
            "payload": {"data": retained} if provider == "marketaux" else retained,
            "coverage_state": state if state != "AVAILABLE" else "AVAILABLE" if retained else "HYGIENE_EXCLUDED" if errors else "BUDGET_NOT_COLLECTED" if omissions else "NO_EVIDENCE",
            "limitations": list(limitations) + result_limit + ["UPSTREAM_QUERY_POLICY_UNCHANGED_NOT_EXHAUSTIVE"],
            "scope": scope, "requested_symbols": list(symbols), "raw_results": original_count,
            "hygiene_excluded": errors, "budget_excluded": omissions})
        self.manifest["recordings"][-1]["hygiene_rejections"] = rejection_reasons
        self.flush()

    def finish(self, report_path=None):
        self.flush()
        report_matches = report_path and Path(report_path).is_file() and json.loads(Path(report_path).read_text(encoding="utf-8"))["market_date"] == self.market_date
        if report_matches:
            from .artifacts import load_archive
            packet = load_archive(report_path, self.market_date, self.root / "v2_captured_recordings.json")
        else:
            packet = build_packet(self.market_date, self.manifest["recordings"])
            packet["diagnostics"]["origin"] = "SYNTHETIC_CAPTURE" if self.manifest["synthetic"] else "CAPTURE_WITHOUT_MARKET_CONTEXT"
        atomic_json(self.root / "v2_evidence_packet.json", packet)
        return packet


def import_historical(market_date, output, *, cache_root, families=None):
    """Only explicit dated caches; never call collectors or fall back to latest."""
    capture = EvidenceCapture(market_date, output)
    allowed = {"marketaux", "finnhub", "verification", "official_rss"}
    selected = set(families) if families is not None else allowed
    if not selected or not selected <= allowed:
        raise ValueError("HISTORICAL_SOURCE_FAMILY_INVALID")
    capture.manifest["capture"].update(selected_cache_families=sorted(selected), excluded_cache_families=sorted(allowed-selected))
    root = Path(cache_root)
    paths = [("marketaux", p) for p in sorted((root/"marketaux"/market_date).glob("*.json"))]
    paths += [("verification", p) for p in sorted((root/"verification"/market_date).glob("*.json"))]
    paths += [("finnhub", p) for p in sorted((root/"finnhub").glob(f"*/{market_date}_*.json"))]
    paths = [(provider, path) for provider, path in paths if provider in selected]
    for provider, path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        capture.manifest["source_files"].append({"file": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if provider == "finnhub":
            if payload.get("requested_from_date") != market_date or payload.get("provider") != "FINNHUB":
                raise ValueError("HISTORICAL_CACHE_DATE_OR_PROVIDER_INVALID")
            payload = payload.get("normalized_candidates", [])
        capture.record(provider, payload, scope=path.stem.rsplit("_", 1)[0].upper() if provider == "marketaux" else "HISTORICAL_CACHE", limitations=["RETRIEVAL_HISTORY_AND_PAGING_NOT_AVAILABLE"])
    from news.official import _rss_candidates, OFFICIAL_FEEDS
    xml_paths = sorted((root/"official_news"/market_date).glob("*.xml")) if "official_rss" in selected else []
    for path in xml_paths:
        feed = OFFICIAL_FEEDS.get(path.stem)
        if feed is None:
            continue
        capture.manifest["source_files"].append({"file": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        name, domain, url = feed
        token = _observer.set(capture.record)
        before = len(capture.manifest["recordings"])
        try:
            _rss_candidates(path.read_text(encoding="utf-8"), name, domain, url, "MACRO", market_date)
        finally:
            _observer.reset(token)
        if len(capture.manifest["recordings"]) == before:
            capture.record("official_rss", [], state="UNAVAILABLE", limitations=["EMPTY_OR_MALFORMED_CACHED_FEED"])
    if not paths and not xml_paths:
        capture.record("historical_cache", [], state="UNAVAILABLE", limitations=["NO_DATED_SOURCE_CACHE_FOUND"])
    capture.flush()
    return capture


def main():
    """Prepare from saved files only. A collection run is never invoked by this CLI."""
    import argparse
    from .session import generate_package
    from .openai_provider import Pricing
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--report", required=True, help="Saved report JSON or production artifact ZIP")
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--families", nargs="+", choices=("marketaux", "finnhub", "verification", "official_rss"), help="Explicit offline subset only; default imports all available families")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--pricing", default=str(Path(__file__).parent/"config/pricing_example.json"))
    args = parser.parse_args()
    from contextlib import ExitStack
    from unittest.mock import patch
    def blocked(*a, **kw):
        raise RuntimeError("HISTORICAL_IMPORT_NETWORK_BLOCKED")
    with ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "socket.create_connection"):
            stack.enter_context(patch(target, blocked))
        capture = import_historical(args.market_date, Path(args.output)/"capture", cache_root=args.cache_root, families=args.families)
        result = generate_package(mode="real-evidence", output=Path(args.output)/"review", market_date=args.market_date,
            archive=args.report, recordings=capture.root/"v2_captured_recordings.json", model=args.model,
            pricing=Pricing(**json.loads(Path(args.pricing).read_text(encoding="utf-8"))))
        print("RESEARCH_READINESS="+result["readiness"]["status"])
        print("USABLE_SOURCE_ITEMS="+str(result["coverage"]["usable_text_items"]))
        print("LIVE_REQUESTS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read genuine archived report ZIPs without extracting files or requesting providers."""
import hashlib
import json
import zipfile
from pathlib import Path

from .evidence import build_packet
from .context import peer_context

PRIORITY_SYMBOLS = ("ORCL", "MU", "LRCX", "AMAT", "NOW")


def load_archive(path, market_date, recordings_path=None):
    archive = Path(path)
    if archive.suffix.lower() == ".json":
        report = json.loads(archive.read_text(encoding="utf-8-sig"))
    else:
        with zipfile.ZipFile(archive) as z:
            members = [i for i in z.infolist() if Path(i.filename).name == "daily_report_data.json"]
            if len(members) != 1 or members[0].file_size > 10000000:
                raise ValueError("ARCHIVE_REPORT_MISSING_AMBIGUOUS_OR_OVERSIZED")
            report = json.loads(z.read(members[0]).decode("utf-8-sig"))
    if report.get("market_date") != market_date:
        raise ValueError("ARCHIVE_MARKET_DATE_MISMATCH")
    if report.get("synthetic") or "SYNTHETIC" in str(report.get("origin", "")).upper():
        raise ValueError("SYNTHETIC_ARCHIVE_NOT_REAL")
    mover_rows = report.get("movers", {})
    movers = []
    for group in ("top_gainers", "top_losers"):
        for row in mover_rows.get(group, []):
            movers.append({k: row[k] for k in ("symbol", "company_name", "close", "change_pct", "volume", "latest_bar_datetime") if k in row})
    if not movers:
        raise ValueError("ARCHIVE_MOVERS_MISSING")
    recordings, files, content = [], [{"file": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}], {}
    if recordings_path:
        saved = Path(recordings_path)
        content = json.loads(saved.read_text(encoding="utf-8-sig"))
        if content.get("origin") != "ARCHIVED_PROVIDER_RESPONSES":
            raise ValueError("RECORDINGS_PROVENANCE_REQUIRED")
        if content.get("synthetic"):
            raise ValueError("SYNTHETIC_RECORDINGS_NOT_REAL")
        if content.get("market_date") and content["market_date"] != market_date:
            raise ValueError("RECORDINGS_MARKET_DATE_MISMATCH")
        recordings = content["recordings"]
        files.append({"file": saved.name, "sha256": hashlib.sha256(saved.read_bytes()).hexdigest()})
    # Public translated summaries and V1 rejected candidate metadata are not original source text.
    # Real provider recordings are a separate explicit input, never reconstructed from prose.
    if not recordings:
        recordings = [{"provider": "archived_news", "coverage_state": "UNAVAILABLE", "payload": [],
                       "limitations": ["ORIGINAL_PROVIDER_SNAPSHOTS_NOT_INCLUDED_IN_REPORT_ZIP"]}]
    context = {k: report[k] for k in ("market_overview", "indices", "macro", "sectors") if k in report}
    context["entity_aliases"] = {r["symbol"]: [r["company_name"]] for r in movers if r.get("company_name")}
    mapping_path = Path(__file__).resolve().parents[1] / "drivers/config/sector_mappings.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    membership = {group: symbols for group, symbols in mapping.get("sectors", {}).items() if any(m["symbol"] in symbols for m in movers)}
    sector_rows = report.get("sectors", {}).get("all", [])
    benchmarks = {r["symbol"]: r["change_pct"] for r in sector_rows if r.get("change_pct") is not None}
    securities = [{"symbol": m["symbol"], "daily_return": m["change_pct"], "volume": m.get("volume"),
        "sector_etf": next((etf for etf, group in mapping.get("sector_aliases", {}).items() if m["symbol"] in membership.get(group, [])), None)} for m in movers if m.get("change_pct") is not None]
    context.update(peer_membership=membership, benchmarks=benchmarks, securities=securities,
                   quantitative_peer_context=peer_context(securities, membership, benchmarks),
                   peer_mapping_provenance={"file": "drivers/config/sector_mappings.json", "sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
                                            "as_of": "UNSPECIFIED_STATIC_CLASSIFICATION_NOT_HISTORICAL_PROOF"})
    packet = build_packet(market_date, recordings, market_context=context, movers=movers)
    packet["diagnostics"].update(origin="REAL_ARCHIVED_REPORT", source_files=files,
        capture=content.get("capture", {}), captured_source_files=content.get("source_files", []),
        acquisition_coverage=[{k: v for k, v in r.items() if k != "payload"} for r in recordings],
        archived_news_status=report.get("news_coverage_status"),
        limitations=["NO_INFERRED_PEER_MEMBERSHIP_OR_HISTORICAL_METRICS", "PUBLIC_STORY_TEXT_NOT_ASSUMED_ORIGINAL_PROVIDER_CONTENT"])
    return packet


def assess_coverage(packet):
    symbols = list(dict.fromkeys([*PRIORITY_SYMBOLS, *[m["symbol"] for m in packet["movers"]]]))
    rows = []
    for symbol in symbols:
        items = [e for e in packet["evidence"] if symbol in e["symbols"]]
        rows.append({"symbol": symbol, "mover_available": any(m["symbol"] == symbol for m in packet["movers"]),
            "association_not_subject_verified": True,
            "observed_query_scopes": [{k: r.get(k) for k in ("provider", "scope", "coverage_state", "limitations")} for r in packet["diagnostics"].get("acquisition_coverage", []) if symbol in r.get("requested_symbols", [])],
            "articles": sum(e["source_category"] != "FILING" for e in items), "official_filings": sum(e["source_category"] == "FILING" for e in items),
            "missing_article_bodies": sum(e["content_completeness"] != "FULL_TEXT" for e in items) if items else None,
            "status": "AVAILABLE" if items and all(e["source_text"] for e in items) else "INCOMPLETE_CONTENT" if items else "ACQUISITION_GAP_NOT_NO_CATALYST",
            "sources": [{"evidence_id": e["evidence_id"], "publisher": e["publisher"], "url": e["original_url"], "published_at": e["published_at"],
                         "text_length": len(e["source_text"]), "completeness": e["content_completeness"],
                         "source_quality": e.get("source_provenance", {}).get("kind", "UNKNOWN"), "limitations": e["provider_limitations"]} for e in items]})
    return {"market_date": packet["market_date"], "origin": packet["diagnostics"].get("origin"), "movers": rows,
            "provider_coverage": packet["coverage"], "acquisition_diagnostics": packet["diagnostics"].get("acquisition_coverage", []),
            "usable_text_items": sum(bool(e["source_text"].strip()) for e in packet["evidence"]),
            "reasoning_testable": any(e["source_text"].strip() for e in packet["evidence"]), "live_provider_calls": 0,
            "unassigned_evidence_count": sum(not e["symbols"] for e in packet["evidence"])}

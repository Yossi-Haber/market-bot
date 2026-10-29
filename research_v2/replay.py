"""python -m research_v2.replay --packet packet.json --response fake.json --output directory"""
import argparse
import json
import socket
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from .research import FakeProvider, ResearchRunner
from .validation import validate_ledger


def replay(packet_path, response_path, output, source_policy_path=None):
    def blocked(*args, **kwargs):
        raise RuntimeError("V2_REPLAY_NETWORK_BLOCKED")
    with ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "socket.create_connection"):
            stack.enter_context(patch(target, blocked))
        packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        response = json.loads(Path(response_path).read_text(encoding="utf-8"))
        result = ResearchRunner(FakeProvider(response), model="synthetic-replay", max_total_tokens=100000).run(packet)
        policy = json.loads(Path(source_policy_path).read_text(encoding="utf-8")) if source_policy_path else {}
        ledger = validate_ledger(packet, result["research"], source_policy=policy) if result["status"] == "PROPOSAL" else {
            "ledger_version": 2, "status": "REJECTED_RESEARCH", "audit_trail": [{"reason": result["status"]}]}
        root = Path(output)
        if root.resolve() in (Path(packet_path).resolve().parent, Path(response_path).resolve().parent):
            raise ValueError("REPLAY_OUTPUT_MUST_BE_SEPARATE")
        root.mkdir(parents=True, exist_ok=True)
        (root / "research_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "validated_research_ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "validation_audit_trail.json").write_text(json.dumps(ledger["audit_trail"], ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "diagnostics.json").write_text(json.dumps({"live_requests": 0, "delivery_calls": 0, "simulated": True, "status": result["status"], "usage": result.get("diagnostics", {})}), encoding="utf-8")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("packet", "response", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--source-policy", help="Caller-owned source authority policy; never supplied by research output")
    args = parser.parse_args()
    result = replay(args.packet, args.response, args.output, args.source_policy)
    print("V2_REPLAY=" + result["status"])
    return 0 if result["status"] == "PROPOSAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())

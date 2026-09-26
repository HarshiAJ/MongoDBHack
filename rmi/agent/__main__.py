"""Command line for purchasing cases.

    python -m rmi.agent review                      # scan, investigate, propose; pauses for approval
    python -m rmi.agent show CASE-...               # what is waiting for approval
    python -m rmi.agent approve CASE-... [ids ...]  # approve all, or only the given finding ids
    python -m rmi.agent reject CASE-...             # reject everything and close
    python -m rmi.agent respond V-100101 file.txt   # a counterparty replied: extract the agreement
"""

import argparse
import json
from pathlib import Path

from rmi.agent import case


def show_pending(payload):
    if not payload:
        print("Nothing waiting for approval.")
        return
    print(f"\n{payload['case_id']} is waiting for approval ({payload['kind']}):\n")
    for i, item in enumerate(payload["items"]):
        if payload["kind"] == "proposals":
            print(f"[{item['finding_id']}] {item['action_type']} -> {item['counterparty_id']} "
                  f"(EUR {item['value_eur']:,.0f}, deadline {item['deadline']})")
            print(f"    {item['subject']}")
            print(f"    why: {item['rationale']}")
        else:
            print(f"[{i}] {json.dumps(item, default=str)}")
    print()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("review")
    s = sub.add_parser("show"); s.add_argument("case_id")
    a = sub.add_parser("approve"); a.add_argument("case_id"); a.add_argument("ids", nargs="*", type=int)
    r = sub.add_parser("reject"); r.add_argument("case_id")
    q = sub.add_parser("respond"); q.add_argument("party_id"); q.add_argument("file")
    args = p.parse_args()

    if args.cmd == "review":
        case_id, payload = case.start({"type": "scheduled_review"})
        show_pending(payload)
    elif args.cmd == "respond":
        case_id, payload = case.start({"type": "counterparty_response", "party_id": args.party_id,
                                       "text": Path(args.file).read_text()})
        show_pending(payload)
    elif args.cmd == "show":
        show_pending(case.state(args.case_id)["pending"])
    elif args.cmd in ("approve", "reject"):
        if args.cmd == "reject":
            decision = {"decisions": []}
        elif args.ids:
            decision = {"decisions": [{"finding_id": i, "decision": "approve"} for i in args.ids]}
        else:
            decision = {"approve_all": True}
        result = case.resume(args.case_id, decision)
        print(result.get("summary", ""))


if __name__ == "__main__":
    main()

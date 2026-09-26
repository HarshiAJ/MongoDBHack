"""Reset the database to a clean demo state.

    .venv/bin/python scripts/reset_demo.py              # clean slate (about 4 minutes)
    .venv/bin/python scripts/reset_demo.py --with-case  # also open a review case waiting for approval

Clears everything the agent wrote (cases, actions, accepted agreements, checkpoints, memory),
reloads the seven sources, re-extracts the contracts and builds a baseline latest estimate.
"""

import argparse
import time

from rmi.db import get_db

AGENT_COLLECTIONS = ["agent_cases", "agent_actions", "contract_changes", "agent_checkpoints",
                     "agent_checkpoint_writes"]


def main(with_case: bool):
    t0 = time.time()
    db = get_db()
    for name in AGENT_COLLECTIONS:
        n = db[name].delete_many({}).deleted_count
        print(f"cleared {name}: {n}")
    n = db.agent_memory.delete_many({}).deleted_count  # keep the collection so its vector index survives
    print(f"cleared agent_memory: {n}")

    print("\n1/3 Reloading the seven sources and search indexes...")
    from rmi.ingest.__main__ import run
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        run()

    print("2/3 Extracting contract terms (LLM)...")
    from rmi import extract
    with contextlib.redirect_stdout(io.StringIO()):
        extract.main(all_docs=True)

    print("3/3 Building the baseline latest estimate...")
    from rmi.engine.plan import reforecast
    t = reforecast(db, reason="baseline latest estimate")["totals"]
    print(f"    budget {t['budget']['contribution']:,.0f}  latest {t['le']['contribution']:,.0f}  "
          f"gap {t['contribution_delta']:,.0f}")

    if with_case:
        print("Opening a review case (waits for approval in the app)...")
        from rmi.agent import case
        case_id, payload = case.start({"type": "scheduled_review"})
        print(f"    {case_id}: {len(payload['items'])} proposals waiting")

    print(f"\nDemo data reset in {time.time() - t0:.0f}s. Refresh http://localhost:8000")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--with-case", action="store_true", help="open a review case waiting for approval")
    main(p.parse_args().with_case)

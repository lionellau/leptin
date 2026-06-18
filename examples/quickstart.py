"""Leptin quickstart — run with: python examples/quickstart.py

Demonstrates the full loop: remember (with dedup/merge + supersede), budgeted
recall, the savings ledger, and guardrailed compaction — all offline.
"""

from leptin.api import Leptin
from leptin.config import Config


def main() -> None:
    mem = Leptin(":memory:", config=Config())

    # 1) Remember — restatements merge, contradictions supersede.
    mem.remember("The user prefers dark mode in every app.", subject="prefs")
    print("dup ->", mem.remember("The user prefers dark mode in every app.", subject="prefs"))
    mem.remember("The backend is FastAPI on Postgres.", subject="stack")
    mem.remember("The free trial lasts 14 days.", subject="billing")
    print("contradiction ->", mem.remember("The free trial now lasts 30 days.", subject="billing"))

    # 2) Recall under a token budget — packed, not dumped.
    res = mem.recall("how long is the trial?", token_budget=400)
    print("\nrecall:", [m["content"] for m in res["memories"]],
          f"(used {res['tokens_used']} tokens, saved {res['tokens_saved']})")

    # 3) Show the receipts.
    report = mem.diet_report("all")
    print(f"\nledger: saved {report['tokens_saved']} tokens "
          f"(${report['usd_saved']}) — ops {report['ops']}")

    # 4) Guardrailed compaction (no-op here; everything is fresh).
    print("compact:", mem.compact())

    mem.close()


if __name__ == "__main__":
    main()

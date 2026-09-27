"""Build a playbook that is the seed plus one extra always-on rule.

Used to test a single core rule in isolation. Everything else is held at the seed, so a difference
in the results is attributable to the added line and not to accumulated lessons or dataset detail.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from benthic_mcp.seed import seed_playbook  # noqa: E402

ANSWER_RULE = (
    "You have a limited number of turns: once you have the rows you need, stop calling tools and "
    "write the final answer. An unanswered question scores zero even if every call succeeded."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", default=ANSWER_RULE)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base", help="an existing playbook to add the rule to, instead of the seed")
    args = parser.parse_args()

    if args.base:
        document = json.loads(Path(args.base).read_text(encoding="utf-8"))
        document["core"] = [args.rule, *document.get("core", [])]
        document["generator"] = f"{document.get('generator', 'unknown')} + answer rule"
    else:
        document = json.loads(seed_playbook().to_json())
        document["core"] = [args.rule, *document["core"]]
        document["generator"] = "seed + answer rule"

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{out}: {len(document['core'])} core line(s), generator={document['generator']}")
    for line in document["core"]:
        print(f"  - {line[:100]}")


if __name__ == "__main__":
    main()

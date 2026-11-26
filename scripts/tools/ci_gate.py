"""CI's two decisions that are easy to get wrong in YAML (ENGINEERING_PATTERNS.md pattern 107). Standard library only: it runs before any install.

    git diff --name-only BASE HEAD | python3 scripts/tools/ci_gate.py changes      # prints `code=true|false` for $GITHUB_OUTPUT
    NEEDS='${{ toJSON(needs) }}' EVENT=push python3 scripts/tools/ci_gate.py verdict   # exit 1 unless the run passed

`changes`: a pull request that only touches documentation skips the slow jobs. `verdict`: the `ci-ok` job, the ONE required check, so a job added
to ci.yml is required by being listed there (a test holds the list complete) instead of by remembering to edit the branch ruleset.
"""
import json
import os
import re
import sys
from typing import Any

# Documentation only. NOT every `*.md`: a domain's skills/<name>/SKILL.md and subagents/<name>/AGENT.md are what the agent runs.
DOCS = re.compile(r"^(docs|specs|\.specify|\.claude)/|^packages/[^/]+/(docs|specs)/|(^|/)(README|CLAUDE)\.md$")
# The jobs a documentation-only pull request skips: the slow ones. The hermetic tests still run, since some read the documents.
SLOW = frozenset({"test-integration", "test-live", "promptfoo", "image"})


def changes_code(paths: list[str]) -> bool:
    """True unless every changed path is documentation. No paths at all is treated as code: an empty diff is a failed lookup, not a docs change."""
    return not paths or not all(DOCS.search(path) for path in paths)


def failures(needs: dict[str, Any], event: str) -> list[str]:
    """What stops the run from passing. A `skipped` job passes only where skipping it was decided: anything else skipped means something upstream broke
    or a condition is wrong, and a required check that accepts that proves nothing."""
    allowed = set()
    if event != "pull_request":
        allowed.add("openapi")  # compares a pull request with main: there is no "before" on a push
    if needs.get("changes", {}).get("outputs", {}).get("code") == "false":
        allowed |= SLOW
    return [
        f"{name}: {job['result']}"
        for name, job in sorted(needs.items())
        if job["result"] != "success" and not (job["result"] == "skipped" and name in allowed)
    ]


def main(argv: list[str]) -> int:
    if argv == ["changes"]:
        print(f"code={'true' if changes_code([line for line in sys.stdin.read().splitlines() if line]) else 'false'}")
        return 0
    if argv == ["verdict"]:
        needs = json.loads(os.environ["NEEDS"])
        for name, job in sorted(needs.items()):
            print(f"{job['result']:<10} {name}")
        found = failures(needs, os.environ["EVENT"])
        for failure in found:
            print(f"::error::{failure}")
        return 1 if found or not needs else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

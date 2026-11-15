"""`SessionStore.list_inactive` is the one session method that spans tenants and people, on purpose: retention is a property of the table, and the sweep that
calls it is an operator's job (`scripts/jobs/checkpoint_sweep.py`, PRODUCTION_REVIEW.md R11). A route, a channel or an MCP server that called it would be a
cross-tenant read of every conversation's owner and age (Principle I), so no driving adapter may name it; the sweep reaches it through
`app/agent/runtime/retention.py` only."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CROSS_TENANT = ("list_inactive", "sweep_inactive_conversations")


def test_no_driving_adapter_names_the_cross_tenant_session_listing_or_the_sweep():
    offenders = [
        f"{path.relative_to(REPO)}: {name}"
        for path in (REPO / "app" / "adapters" / "inbound").rglob("*.py")
        for name in CROSS_TENANT
        if name in path.read_text(encoding="utf-8")
    ]

    assert offenders == []

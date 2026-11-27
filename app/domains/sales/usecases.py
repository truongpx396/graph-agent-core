"""The sales module's published use cases: what a driving adapter outside it calls (today the scheduled follow-up sweep, `scripts/jobs/followup_sweep.py`).

The tool layer (`tools.py`) is the agent's way in; the sweep is an unattended job that never runs an agent turn, so it reads and closes follow-ups, and reads
what is on record about a lead, through the `LeadStore` port (`ports.py`) from here. Both scope by `tenant`, as every method of the port does.
"""
from datetime import datetime
from typing import Any

from app.domains.sales.ports import lead_store

# What `package_lead_brief` will replay of a lead's notes, in characters. A single
# website-research note can be 20,000 characters (the crawler's own cap) and every
# `enrich_lead_from_website` adds another, so an uncapped replay grew with each
# enrichment — 59,150 characters after three. Roughly 2,000 tokens is room for
# the notes a rep actually wrote plus the newest research, and a small model's
# context is not the place for the rest.
BRIEF_NOTES_MAX_CHARS = 8_000


def bounded_notes(notes: str) -> str:
    """The newest `BRIEF_NOTES_MAX_CHARS` of `notes` (they are aggregated
    oldest-first), with the cut stated so a reader doesn't take the start of
    the text for the start of the history. Cuts the aggregate, not each note:
    the aggregate is one string by the time it reaches here, and the newest
    material is what a handoff needs."""
    if len(notes) <= BRIEF_NOTES_MAX_CHARS:
        return notes
    return f"[earlier notes omitted — the newest {BRIEF_NOTES_MAX_CHARS} characters follow]\n{notes[-BRIEF_NOTES_MAX_CHARS:]}"


async def due_followups(tenant: str, as_of: datetime) -> list[dict[str, Any]]:
    """Pending follow-ups due by `as_of`, each with its lead's name and contact, oldest first."""
    return await lead_store.get().due_followups(tenant, as_of)


async def mark_followup_done(tenant: str, followup_id: int) -> None:
    """Closes a swept follow-up so the next run does not draft it again."""
    await lead_store.get().mark_followup_done(tenant, followup_id)


async def lead_notes(tenant: str, contact: str) -> str:
    """What is on record about a lead, bounded like the brief's own replay; "" when there is nothing or no such lead."""
    history = await lead_store.get().lead_history(tenant, contact)
    return bounded_notes(history["notes"]) if history and history["notes"] else ""

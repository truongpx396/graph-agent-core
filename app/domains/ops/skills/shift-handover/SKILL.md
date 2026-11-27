---
name: shift-handover
description: Write a shift handover or standup status summary for the next on-call engineer, from current metric readings and every open incident. Use for "handover", "status summary", "standup update" or "what should the next person know".
---

# Shift Handover

A handover reports. It does not investigate and it does not change anything.

1. Call `fetch_metrics_summary` once.
2. Call `list_recent_incidents` with status `open`.
3. Write the handover under these three headings:
   - **Now**: each metric past its threshold, with its number. If a metric could not be read, say so: unread is not the same as healthy.
   - **Open incidents**: number, summary and who opened it. "None open" if there are none.
   - **Next person should**: at most three actions that follow from the two lists above. Metrics that could not be read are an action ("check the metrics store"), not a clean result; write "nothing to do" only when every metric was read and is within its threshold and no incident is open.
4. Do not call `log_incident` or `resolve_incident` here. Call `post_to_team_channel` only if the user asked for the handover to be posted.

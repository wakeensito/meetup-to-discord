# Agent notes: meetup-to-discord

Single-purpose bot: new Meetup event → one Discord message with a task checklist, then a
reminder 3 days out and the morning of. Zero AWS, zero dependencies.

## Resume here
- Source of truth for behavior: `meetup_to_discord.py` (one file) and `tests/` (unittest, stdlib).
- Data source is the group's **iCal** feed, not RSS. iCal has start/end times and the event ID
  in `UID`; RSS does not. Do not switch back without checking `tests/fixtures/events.ics`.
- Venue comes from the event page's schema.org JSON-LD (`location[]` with a `Place`), room from
  the `Room:` line in the description. Both best-effort; the 📍 line is dropped if both fail.
- State: `seen_events.json` in the repo root, committed back by `.github/workflows/poll.yml`.
  **Do not enable branch protection on main**; the Actions token cannot push through it. If
  protection is ever wanted, move the state file to an unprotected `state` branch.
- Reminder windows live in `decide_actions` / `apply_action`. Day-of opens at 8:00 AM ET and takes
  precedence over a missed 3-day reminder.
- A weekly heartbeat commit (`_last_checked`) keeps the repo "active" so GitHub does not disable
  the schedule after 60 days of inactivity.

## Working rules
- TDD: every behavior change gets a failing test first. `python3 -m unittest discover -s tests`.
- Local manual check: `python3 meetup_to_discord.py --dry-run --now 2026-10-19T10:00`.
- Never commit the webhook URL. It is the `DISCORD_WEBHOOK_URL` Actions secret. The optional
  role ping is the `DISCORD_MENTION_ROLE_ID` repo *variable*; it is whitelisted in
  `allowed_mentions.roles`, everything else stays unpingable.

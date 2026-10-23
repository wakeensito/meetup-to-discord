# Agent notes: meetup-to-discord

Single-purpose bot, two audiences. New Meetup event → E-board channel gets a task checklist,
`#announcements` gets a member announcement. Members then get a 3-day and a day-of reminder.
The morning after, E-board gets the post-event checklist and members get a thank-you with the
survey link. Zero AWS, zero dependencies.

## Resume here
- Source of truth for behavior: `meetup_to_discord.py` (one file) and `tests/` (unittest, stdlib).
- Data source is the group's **iCal** feed, not RSS. iCal has start/end times and the event ID
  in `UID`; RSS does not. Do not switch back without checking `tests/fixtures/events.ics`.
- Venue comes from the event page's schema.org JSON-LD (`location[]` with a `Place`), room from
  the `Room:` line in the description. Both best-effort; the 📍 line is dropped if both fail.
- State: `seen_events.json` in the repo root, committed back by `.github/workflows/poll.yml`.
  **Do not enable branch protection on main**; the Actions token cannot push through it. If
  protection is ever wanted, move the state file to an unprotected `state` branch.
- Routing is `MEMBER_KINDS` in `decide_actions` / `run()`: `member_announce`, `remind_3d`,
  `remind_day_of`, `member_thanks` go to `#announcements`; `announce`, `post_event` to E-board.
  Each has its own state flag. Windows: day-of opens 8:00 AM ET and beats a missed 3-day;
  morning-after posts open 8:00 AM ET the next day and close 7 days after start.
- Morning-after posts are driven by **saved state**, not the feed (Meetup drops past events), so
  `url`, `survey_url` and the flags must be in every entry. `run()` backfills older entries with
  member flags set to `True` so members are never retro-announced or thanked.
- Member templates use blank lines between blocks (`_blocks`). No 👉 emoji; the user vetoed it.
- A weekly heartbeat commit (`_last_checked`) keeps the repo "active" so GitHub does not disable
  the schedule after 60 days of inactivity.

## Working rules
- TDD: every behavior change gets a failing test first. `python3 -m unittest discover -s tests`.
- Local manual check: `python3 meetup_to_discord.py --dry-run --now 2026-10-19T10:00`.
- Never commit webhook URLs. They are the `DISCORD_WEBHOOK_URL` (E-board) and
  `DISCORD_ANNOUNCE_WEBHOOK_URL` (#announcements) Actions secrets. Role pings are the
  `DISCORD_MENTION_ROLE_ID` / `DISCORD_ANNOUNCE_ROLE_ID` repo *variables*; each is whitelisted in
  `allowed_mentions.roles` for its own channel, everything else stays unpingable.
- Preview without upcoming events: `--feed tests/fixtures/events.ics --state /tmp/s.json`.

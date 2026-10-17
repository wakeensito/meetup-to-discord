"""Tests for meetup_to_discord. Run: python3 -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meetup_to_discord as m  # noqa: E402

ET = ZoneInfo("America/New_York")
FIX = Path(__file__).parent / "fixtures"
ICS = (FIX / "events.ics").read_text(encoding="utf-8")
PAGE = (FIX / "event_page.html").read_text(encoding="utf-8")
EVENT_URL = "https://www.meetup.com/aws-sbg-at-florida-international-university/events/316843450/"


def et(*args):
    return datetime(*args, tzinfo=ET)


def sample_event(**over):
    base = dict(
        id="316843450",
        title="AWS Certifications & Career Success Workshop",
        start=et(2026, 10, 22, 15, 0),
        end=et(2026, 10, 22, 17, 0),
        url=EVENT_URL,
        description="Intro\n\n**Room: INV1 - 201**\n\nMore",
    )
    base.update(over)
    return m.Event(**base)


class ParseIcalTests(unittest.TestCase):
    def test_parses_real_feed_into_one_event_with_et_times(self):
        events = m.parse_ical(ICS)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.id, "316843450")
        self.assertEqual(ev.title, "AWS Certifications & Career Success Workshop")
        self.assertEqual(ev.start, et(2026, 10, 22, 15, 0))
        self.assertEqual(ev.end, et(2026, 10, 22, 17, 0))
        self.assertEqual(ev.url, EVENT_URL)

    def test_unfolds_and_unescapes_description(self):
        ev = m.parse_ical(ICS)[0]
        self.assertIn("**Room: INV1 - 201**", ev.description)
        self.assertIn("certifications, building practical cloud skills", ev.description)
        self.assertNotIn("\\,", ev.description)

    def test_utc_dtstart_converted_to_et_and_missing_dtend_is_none(self):
        ics = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event_123@meetup.com\n"
            "DTSTART:20261022T190000Z\nSUMMARY:X\nURL;VALUE=URI:https://www.meetup.com/g/events/123/\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        ev = m.parse_ical(ics)[0]
        self.assertEqual(ev.start, et(2026, 10, 22, 15, 0))
        self.assertIsNone(ev.end)
        self.assertEqual(ev.id, "123")

    def test_id_falls_back_to_url_digits_when_uid_has_none(self):
        ics = (
            "BEGIN:VEVENT\nUID:weird@meetup.com\nDTSTART;TZID=America/New_York:20261022T150000\n"
            "SUMMARY:X\nURL;VALUE=URI:https://www.meetup.com/g/events/987/\nEND:VEVENT\n"
        )
        self.assertEqual(m.parse_ical(ics)[0].id, "987")


class LocationTests(unittest.TestCase):
    def test_extract_room_from_description(self):
        self.assertEqual(m.extract_room("x\n**Room: INV1 - 201**\ny"), "INV1 - 201")

    def test_extract_room_none_when_absent(self):
        self.assertIsNone(m.extract_room("no room here"))

    def test_parse_venue_picks_place_name_from_json_ld(self):
        self.assertEqual(
            m.parse_venue(PAGE), "Florida International University (Modesto main campus)"
        )

    def test_parse_venue_none_on_garbage(self):
        self.assertIsNone(m.parse_venue("<html></html>"))

    def test_format_location_combinations(self):
        self.assertEqual(m.format_location("FIU", "INV1 - 201"), "FIU · Room: INV1 - 201")
        self.assertEqual(m.format_location(None, "INV1 - 201"), "Room: INV1 - 201")
        self.assertEqual(m.format_location("FIU", None), "FIU")
        self.assertIsNone(m.format_location(None, None))


class FormatTests(unittest.TestCase):
    def test_announcement_matches_template(self):
        text = m.format_announcement(sample_event(), "FIU · Room: INV1 - 201")
        self.assertTrue(text.startswith("📣 **New event is up on Meetup!**\n\n"))
        self.assertIn("**AWS Certifications & Career Success Workshop**\n", text)
        self.assertIn("📅 Thursday, October 22 · 3:00 PM–5:00 PM ET\n", text)
        self.assertIn("📍 FIU · Room: INV1 - 201\n", text)
        self.assertIn(f"🔗 {EVENT_URL}\n", text)
        self.assertIn("**Marketing & Outreach:**\n- [ ] Discord — 3 posts", text)
        self.assertIn("**Tabling & Promotion:**", text)
        self.assertIn("**Technical Prep (Workshop Leads):**", text)
        self.assertTrue(text.endswith("Claim an item by replying with the task + your name!"))
        self.assertLessEqual(len(text), 2000)
        self.assertNotIn("None", text)

    def test_announcement_omits_end_time_when_missing(self):
        text = m.format_announcement(sample_event(end=None), "FIU")
        self.assertIn("📅 Thursday, October 22 · 3:00 PM ET\n", text)
        self.assertNotIn("–", text.split("\n")[3])

    def test_announcement_omits_location_line_when_missing(self):
        text = m.format_announcement(sample_event(), None)
        self.assertNotIn("📍", text)
        self.assertNotIn("None", text)

    def test_announcement_hard_capped_at_2000(self):
        text = m.format_announcement(sample_event(title="T" * 3000), None)
        self.assertLessEqual(len(text), 2000)
        self.assertTrue(text.endswith("…"))

    def test_reminder_3d(self):
        text = m.format_reminder(sample_event(), "remind_3d", "FIU")
        self.assertIn("⏰", text)
        self.assertIn("AWS Certifications & Career Success Workshop", text)
        self.assertIn("Thursday, October 22 at 3:00 PM ET", text)
        self.assertIn(EVENT_URL, text)

    def test_reminder_day_of(self):
        text = m.format_reminder(sample_event(), "remind_day_of", "FIU · Room: INV1 - 201")
        self.assertIn("Today", text)
        self.assertIn("3:00 PM ET", text)
        self.assertIn("📍 FIU · Room: INV1 - 201", text)

    def test_announcement_mentions_role_on_first_line_when_given(self):
        text = m.format_announcement(sample_event(), None, mention="<@&42>")
        self.assertTrue(text.startswith("<@&42> 📣 **New event is up on Meetup!**\n"))

    def test_reminders_mention_role_on_first_line_when_given(self):
        for kind in ("remind_3d", "remind_day_of"):
            text = m.format_reminder(sample_event(), kind, None, mention="<@&42>")
            self.assertTrue(text.startswith("<@&42> "), text)

    def test_no_mention_by_default(self):
        self.assertNotIn("<@&", m.format_announcement(sample_event(), None))
        self.assertNotIn("<@&", m.format_reminder(sample_event(), "remind_3d", None))

    def test_role_mention_helper(self):
        self.assertEqual(m.role_mention("42"), "<@&42>")
        self.assertIsNone(m.role_mention(""))
        self.assertIsNone(m.role_mention(None))

    def test_reminder_without_location_has_no_pin_line(self):
        text = m.format_reminder(sample_event(), "remind_day_of", None)
        self.assertNotIn("📍", text)
        self.assertNotIn("None", text)


def announced(**over):
    entry = {
        "title": "AWS Certifications & Career Success Workshop",
        "start": "2026-10-22T15:00:00-04:00",
        "announced": True,
        "reminded_3d": False,
        "reminded_day_of": False,
    }
    entry.update(over)
    return {"316843450": entry}


class DecideActionsTests(unittest.TestCase):
    ev = sample_event()

    def kinds(self, state, now):
        return [k for _, k in m.decide_actions([self.ev], state, now)]

    def test_unknown_event_is_announced(self):
        self.assertEqual(self.kinds({}, et(2026, 10, 17, 12)), ["announce"])

    def test_known_event_outside_windows_does_nothing(self):
        self.assertEqual(self.kinds(announced(), et(2026, 10, 17, 12)), [])

    def test_3d_window_opens_at_8am_three_days_before(self):
        self.assertEqual(self.kinds(announced(), et(2026, 10, 19, 7, 59)), [])
        self.assertEqual(self.kinds(announced(), et(2026, 10, 19, 8, 0)), ["remind_3d"])
        self.assertEqual(self.kinds(announced(), et(2026, 10, 20, 9)), ["remind_3d"])

    def test_3d_not_repeated(self):
        self.assertEqual(self.kinds(announced(reminded_3d=True), et(2026, 10, 20, 9)), [])

    def test_day_of_window_opens_at_8am_and_closes_at_start(self):
        st = announced(reminded_3d=True)
        self.assertEqual(self.kinds(st, et(2026, 10, 22, 7, 59)), [])
        self.assertEqual(self.kinds(st, et(2026, 10, 22, 8, 0)), ["remind_day_of"])
        self.assertEqual(self.kinds(st, et(2026, 10, 22, 15, 1)), [])

    def test_day_of_takes_precedence_over_missed_3d(self):
        self.assertEqual(self.kinds(announced(), et(2026, 10, 22, 9)), ["remind_day_of"])

    def test_day_of_not_repeated(self):
        st = announced(reminded_3d=True, reminded_day_of=True)
        self.assertEqual(self.kinds(st, et(2026, 10, 22, 9)), [])

    def test_early_morning_event_window_starts_at_midnight(self):
        ev = sample_event(start=et(2026, 10, 22, 7, 0), end=None)
        st = announced(start="2026-10-22T07:00:00-04:00", reminded_3d=True)
        self.assertEqual([k for _, k in m.decide_actions([ev], st, et(2026, 10, 22, 0, 10))],
                         ["remind_day_of"])


class ApplyActionTests(unittest.TestCase):
    ev = sample_event()

    def test_announce_outside_windows_leaves_reminders_pending(self):
        st = {}
        m.apply_action(st, self.ev, "announce", et(2026, 10, 17, 12))
        e = st["316843450"]
        self.assertTrue(e["announced"])
        self.assertFalse(e["reminded_3d"])
        self.assertFalse(e["reminded_day_of"])
        self.assertEqual(e["start"], "2026-10-22T15:00:00-04:00")
        self.assertEqual(e["title"], self.ev.title)

    def test_announce_inside_3d_window_marks_3d_done(self):
        st = {}
        m.apply_action(st, self.ev, "announce", et(2026, 10, 19, 9))
        self.assertTrue(st["316843450"]["reminded_3d"])
        self.assertFalse(st["316843450"]["reminded_day_of"])

    def test_announce_on_the_day_marks_both_done(self):
        st = {}
        m.apply_action(st, self.ev, "announce", et(2026, 10, 22, 9))
        self.assertTrue(st["316843450"]["reminded_3d"])
        self.assertTrue(st["316843450"]["reminded_day_of"])

    def test_day_of_reminder_also_closes_3d(self):
        st = announced()
        m.apply_action(st, self.ev, "remind_day_of", et(2026, 10, 22, 9))
        self.assertTrue(st["316843450"]["reminded_3d"])
        self.assertTrue(st["316843450"]["reminded_day_of"])


class PruneAndHeartbeatTests(unittest.TestCase):
    def test_prune_drops_old_entries_not_in_feed_only(self):
        now = et(2026, 10, 17, 12)
        st = {
            "1": {"start": (now - timedelta(days=100)).isoformat(), "announced": True},
            "2": {"start": (now - timedelta(days=10)).isoformat(), "announced": True},
            "3": {"start": (now - timedelta(days=100)).isoformat(), "announced": True},
            "_last_checked": "x",
        }
        m.prune_state(st, feed_ids={"3"}, now=now)
        self.assertEqual(set(st), {"2", "3", "_last_checked"})

    def test_heartbeat_touches_only_when_stale(self):
        now = et(2026, 10, 17, 12)
        st = {"_last_checked": (now - timedelta(days=8)).isoformat()}
        self.assertTrue(m.touch_heartbeat(st, now))
        self.assertEqual(st["_last_checked"], now.isoformat())
        self.assertFalse(m.touch_heartbeat(st, now + timedelta(days=1)))
        self.assertTrue(m.touch_heartbeat({}, now))


class PostDiscordTests(unittest.TestCase):
    def resp(self, code):
        r = mock.MagicMock()
        r.status = code
        r.__enter__.return_value = r
        return r

    def http_error(self, code, body=b""):
        return HTTPError("u", code, "err", {}, io.BytesIO(body))

    def test_204_is_success_and_posts_json_with_no_mentions(self):
        with mock.patch.object(m, "urlopen", return_value=self.resp(204)) as uo:
            m.post_discord("https://hook/x", "hello")
        req = uo.call_args.args[0]
        self.assertTrue(req.full_url.startswith("https://hook/x?wait=true"))
        body = json.loads(req.data.decode())
        self.assertEqual(body["content"], "hello")
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_role_id_is_whitelisted_in_allowed_mentions(self):
        with mock.patch.object(m, "urlopen", return_value=self.resp(204)) as uo:
            m.post_discord("https://hook/x", "<@&42> hi", role_id="42")
        body = json.loads(uo.call_args.args[0].data.decode())
        self.assertEqual(body["allowed_mentions"], {"parse": [], "roles": ["42"]})

    def test_429_retries_after_sleeping(self):
        err = self.http_error(429, b'{"retry_after": 0.25}')
        with mock.patch.object(m, "urlopen", side_effect=[err, self.resp(200)]) as uo, \
                mock.patch.object(m.time, "sleep") as sl:
            m.post_discord("https://hook/x", "hello")
        self.assertEqual(uo.call_count, 2)
        sl.assert_called_once_with(0.25)

    def test_500_raises_post_error(self):
        with mock.patch.object(m, "urlopen", side_effect=self.http_error(500, b"boom")), \
                mock.patch.object(m.time, "sleep"):
            with self.assertRaises(m.PostError):
                m.post_discord("https://hook/x", "hello")


class RunFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "seen_events.json"
        self.posted = []
        self.events = m.parse_ical(ICS)
        self._quiet = mock.patch.object(sys, "stdout", io.StringIO())
        self._quiet.start()

    def tearDown(self):
        self._quiet.stop()
        self.tmp.cleanup()

    def poster(self, content):
        self.posted.append(content)

    def go(self, now=et(2026, 10, 17, 12), **kw):
        kw.setdefault("venue_lookup", lambda url: "FIU")
        return m.run(self.events, self.state_path, now, self.poster, **kw)

    def test_seed_writes_state_and_posts_nothing(self):
        self.assertEqual(self.go(seed=True), 0)
        self.assertEqual(self.posted, [])
        st = json.loads(self.state_path.read_text())
        self.assertTrue(st["316843450"]["announced"])
        self.assertFalse(st["316843450"]["reminded_3d"])

    def test_second_run_posts_nothing(self):
        self.go(seed=True)
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.posted, [])

    def test_removing_an_id_posts_exactly_one_announcement(self):
        self.go(seed=True)
        st = json.loads(self.state_path.read_text())
        del st["316843450"]
        self.state_path.write_text(json.dumps(st))
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.posted), 1)
        self.assertIn("📣 **New event is up on Meetup!**", self.posted[0])
        self.assertIn("📍 FIU · Room: INV1 - 201", self.posted[0])
        self.assertTrue(json.loads(self.state_path.read_text())["316843450"]["announced"])

    def test_dry_run_prints_and_writes_nothing(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = self.go(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted, [])
        self.assertIn("New event is up", buf.getvalue())
        self.assertFalse(self.state_path.exists())

    def test_post_failure_exits_nonzero_and_does_not_mark_seen(self):
        def bad(content):
            raise m.PostError("500 boom")
        rc = m.run(self.events, self.state_path, et(2026, 10, 17, 12), bad,
                   venue_lookup=lambda u: None)
        self.assertEqual(rc, 1)
        st = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.assertNotIn("316843450", st)

    def test_partial_progress_persists_when_second_post_fails(self):
        ev2 = sample_event(id="999", title="Second", url="https://www.meetup.com/g/events/999/")
        calls = []

        def flaky(content):
            calls.append(content)
            if len(calls) == 2:
                raise m.PostError("boom")
        rc = m.run(self.events + [ev2], self.state_path, et(2026, 10, 17, 12), flaky,
                   venue_lookup=lambda u: None)
        self.assertEqual(rc, 1)
        st = json.loads(self.state_path.read_text())
        self.assertEqual(sorted(k for k in st if not k.startswith("_")), ["316843450"])

    def test_venue_lookup_failure_still_posts_with_room_only(self):
        def boom(url):
            raise RuntimeError("network")
        rc = m.run(self.events, self.state_path, et(2026, 10, 17, 12), self.poster,
                   venue_lookup=boom)
        self.assertEqual(rc, 0)
        self.assertIn("📍 Room: INV1 - 201", self.posted[0])

    def test_reminders_fire_once_each_across_runs(self):
        self.go(seed=True)
        self.go(now=et(2026, 10, 19, 10))
        self.go(now=et(2026, 10, 19, 10, 15))
        self.go(now=et(2026, 10, 22, 8, 5))
        self.go(now=et(2026, 10, 22, 8, 20))
        self.assertEqual(len(self.posted), 2)
        self.assertIn("⏰", self.posted[0])
        self.assertIn("Today", self.posted[1])

    def test_run_threads_role_mention_into_every_post(self):
        self.go(seed=True)
        self.go(now=et(2026, 10, 19, 10), mention_role_id="42")
        self.go(now=et(2026, 10, 22, 8, 5), mention_role_id="42")
        self.assertEqual(len(self.posted), 2)
        for text in self.posted:
            self.assertTrue(text.startswith("<@&42> "), text)

    def test_heartbeat_written_when_idle_and_stale(self):
        self.go(seed=True)
        self.go(now=et(2026, 10, 25, 12))  # event passed, no actions
        st = json.loads(self.state_path.read_text())
        self.assertEqual(st["_last_checked"], et(2026, 10, 25, 12).isoformat())


class CliTests(unittest.TestCase):
    def test_parse_now_accepts_naive_as_et(self):
        self.assertEqual(m.parse_now("2026-10-19T10:00"), et(2026, 10, 19, 10, 0))

    def test_main_requires_webhook_unless_dry_run(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(m.main(["--state", "/nonexistent/x.json"]), 2)


if __name__ == "__main__":
    unittest.main()

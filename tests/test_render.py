"""Unit tests for the resolver — the only logic in the project. No network, no real files.

Run:  python -m unittest discover tests
"""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render as r  # noqa: E402

DAY = dt.date(2026, 9, 18)
TZ = "Asia/Bangkok"
ME, PARTNER = "me@example.com", "partner@example.com"
PEOPLE = [{"name": "Alex", "emails": [ME]}, {"name": "Sam", "emails": [PARTNER]}]


def ev(summary, start=None, end=None, creator=ME, location="", all_day=None, status=None):
    e = {"summary": summary, "creator": {"email": creator}, "location": location}
    if all_day:
        e["start"], e["end"] = {"date": all_day}, {"date": all_day}
    else:
        e["start"] = {"dateTime": f"{DAY}T{start}:00+07:00"}
        e["end"] = {"dateTime": f"{DAY}T{end}:00+07:00"}
    if status:
        e["status"] = status
    return e


class Classify(unittest.TestCase):
    def test_vocabulary(self):
        for label, kind in [
            ("Easy run 7–8 km", "run"), ("Threshold 3 × 10 min", "run"), ("Long run 12–14 km", "run"),
            ("Recovery run 5–6 km", "run"), ("Strength · lower body", "strength"), ("Rest / mobility", "rest"),
            ("Mobility", "rest"), ("Yoga", "other"), ("Pickleball", "other"),
        ]:
            self.assertEqual(r.classify(label), kind, label)


class Resolve(unittest.TestCase):
    def test_split_by_creator(self):
        rows = r.resolve([ev("Easy run 7–8 km", "18:00", "19:30", location="Track"),
                          ev("Yoga", "19:00", "20:00", creator=PARTNER, location="Studio")], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "EASY RUN 7–8 KM")
        self.assertEqual(rows["Alex"]["time"], "18:00–19:30")
        self.assertEqual(rows["Alex"]["place"], "Track")
        self.assertEqual(rows["Sam"]["label"], "YOGA")
        self.assertEqual(rows["Sam"]["time"], "19:00–20:00")

    def test_unknown_creator_defaults_to_first_person(self):
        rows = r.resolve([ev("Yoga", "19:00", "20:00", creator="service-account@x.iam.gserviceaccount.com")], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "YOGA")
        self.assertIsNone(rows["Sam"])

    def test_prefix_overrides_creator(self):
        rows = r.resolve([ev("Sam: Pilates", "10:00", "11:00")], DAY, PEOPLE, TZ)
        self.assertIsNone(rows["Alex"])
        self.assertEqual(rows["Sam"]["label"], "PILATES")

    def test_all_day_and_empty(self):
        rows = r.resolve([ev("Rest / mobility", all_day=str(DAY))], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "REST / MOBILITY")
        self.assertEqual(rows["Alex"]["time"], "")
        self.assertTrue(rows["Alex"]["all_day"])
        self.assertIsNone(rows["Sam"])
        self.assertEqual(r.resolve([], DAY, PEOPLE, TZ), {"Alex": None, "Sam": None})

    def test_timed_beats_all_day_and_cancelled_ignored(self):
        rows = r.resolve([ev("Rest / mobility", all_day=str(DAY)),
                          ev("Easy run 7–8 km", "18:00", "19:30"),
                          ev("Ghost", "06:00", "07:00", status="cancelled")], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "EASY RUN 7–8 KM")

    def test_earliest_timed_wins(self):
        rows = r.resolve([ev("Strength · upper", "18:00", "18:45"), ev("Recovery run 5–6 km", "07:00", "07:45")], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "RECOVERY RUN 5–6 KM")

    def test_single_person_config(self):
        rows = r.resolve([ev("Yoga", "19:00", "20:00", creator=PARTNER)], DAY, [{"name": "Solo", "emails": []}], TZ)
        self.assertEqual(rows, {"Solo": {"label": "YOGA", "kind": "other", "time": "19:00–20:00", "place": "", "all_day": False}})


class ActivityLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "activity_log.csv"
        self.log.write_text(
            "date,type,name,distance_km,moving_time,pace_min_km,avg_hr,location\n"
            "2026-09-17,weight_training,Lower body,,00:38:10,,90,\n"
            "2026-09-18,run,Easy,7.86,00:52:00,6:38,146,Track\n"
            "2026-09-16,run,Threshold 3x10 min — block opener,9.73,01:11:35,7:21,150,Track\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_done(self):
        self.assertEqual(r.done_state(DAY, "run", self.log), "7.9 KM · 6:38 · HR 146")

    def test_strength_done(self):
        self.assertEqual(r.done_state(dt.date(2026, 9, 17), "strength", self.log), "38 MIN")

    def test_kind_mismatch_rest_and_missing(self):
        self.assertIsNone(r.done_state(DAY, "strength", self.log))
        self.assertIsNone(r.done_state(DAY, "rest", self.log))
        self.assertIsNone(r.done_state(dt.date(2026, 9, 19), "run", self.log))
        self.assertIsNone(r.done_state(DAY, "run", None))
        self.assertIsNone(r.done_state(DAY, "run", Path("/nonexistent.csv")))

    def test_logged_session_when_nothing_planned(self):
        s = r.logged_session(dt.date(2026, 9, 16), self.log)
        self.assertEqual(s["label"], "THRESHOLD 3X10 MIN")
        self.assertEqual(s["kind"], "run")
        self.assertEqual(s["place"], "Track")
        self.assertIsNone(r.logged_session(dt.date(2026, 9, 15), self.log))
        self.assertIsNone(r.logged_session(DAY, None))


class RaceCountdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        g = Path(self.tmp.name)
        (g / "City 10K.md").write_text("---\ntype: goal\nstatus: active\ntarget_date: 2026-11-29\n---\n# x\n")
        (g / "Winter Half.md").write_text("---\nstatus: active\ntarget_date: 2027-01-10\n---\n")
        (g / "Old Race.md").write_text("---\nstatus: achieved\ntarget_date: 2026-07-12\n---\n")
        (g / "Strength Habit.md").write_text("---\nstatus: active\ntarget_date: 2026-11\n---\n")
        self.goals = g

    def tearDown(self):
        self.tmp.cleanup()

    def test_nearest_active_full_date(self):
        c = r.race_countdown(dt.date(2026, 9, 17), self.goals)
        self.assertEqual((c["name"], c["days"]), ("City 10K", 73))

    def test_rolls_to_next_after_race_day(self):
        self.assertEqual(r.race_countdown(dt.date(2026, 11, 30), self.goals)["name"], "Winter Half")

    def test_race_day_is_zero_and_missing_dir_is_none(self):
        self.assertEqual(r.race_countdown(dt.date(2026, 11, 29), self.goals)["days"], 0)
        self.assertIsNone(r.race_countdown(DAY, None))
        self.assertIsNone(r.race_countdown(DAY, Path("/nonexistent")))


class Quotes(unittest.TestCase):
    def test_parse_and_rotate(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "q.md"
            p.write_text('# Quotes\n\nintro line\n- "First one." — A. Person\n- “Second one.” — B. Person\n- Bare quote\n')
            qs = r.load_quotes(p)
            self.assertEqual(qs, [("First one.", "A. Person", set()), ("Second one.", "B. Person", set()), ("Bare quote", "", set())])
            a = r.quote_for(dt.date(2026, 9, 17), qs)
            self.assertEqual(a, r.quote_for(dt.date(2026, 9, 17), qs))
            self.assertNotEqual(a, r.quote_for(dt.date(2026, 9, 18), qs))
            self.assertIsNone(r.quote_for(DAY, []))
            self.assertEqual(r.load_quotes(None), [])

    def test_rest_tag(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "q.md"
            p.write_text('- "Go." — A #Rest\n- "Push." — B\n- "Sleep." — C #rest #calm\n- "Move." — D\n')
            qs = r.load_quotes(p)
            self.assertEqual(qs[0], ("Go.", "A", {"rest"}))  # tag stripped from the author, case-folded
            self.assertEqual(qs[2][2], {"rest", "calm"})
            for d in range(10):
                day = DAY + dt.timedelta(days=d)
                self.assertIn(r.quote_for(day, qs, kind="rest")[0], ("Go.", "Sleep."))
                self.assertIn(r.quote_for(day, qs, kind="run")[0], ("Push.", "Move."))
                self.assertIn(r.quote_for(day, qs)[0], ("Push.", "Move."))  # no kind = training pool
            # no tagged quotes at all -> rest days fall back to the full list
            only_plain = [q for q in qs if not q[2]]
            self.assertIn(r.quote_for(DAY, only_plain, kind="rest")[0], ("Push.", "Move."))


class Build(unittest.TestCase):
    def test_build_uses_config_paths(self):
        with tempfile.TemporaryDirectory() as t:
            log = Path(t) / "log.csv"
            log.write_text("date,type,name,distance_km,pace_min_km,avg_hr\n2026-09-18,run,Easy,7.86,6:38,146\n")
            cfg = {"timezone": TZ, "people": [{"name": "Alex", "emails": [ME], "activity_log": str(log)}, {"name": "Sam", "emails": [PARTNER]}]}
            data = r.build(DAY, [ev("Easy run", "18:00", "19:30")], cfg, now=dt.datetime(2026, 9, 18, 6, 0))
            self.assertEqual(data["date_label"], "FRI 18 SEP")
            self.assertEqual(data["rows"][0]["done"], "7.9 KM · 6:38 · HR 146")
            self.assertIsNone(data["rows"][1]["session"])
            self.assertIsNone(data["race"])
            self.assertIsNone(data["quote"])
            self.assertEqual(data["updated"], "06:00")


class Render(unittest.TestCase):
    def test_renders_1bit_800x480_for_every_fixture(self):
        sess = lambda label, kind, time="", place="", all_day=False: {"label": label, "kind": kind, "time": time, "place": place, "all_day": all_day}  # noqa: E731
        cases = [
            {"rows": [{"name": "Alex", "session": None, "done": None}, {"name": "Sam", "session": None, "done": None}], "race": None, "quote": None},
            {"rows": [{"name": "Alex", "session": sess("THRESHOLD 3 × 10 MIN", "run", "18:00–19:30", "Track"), "done": "9.7 KM · 7:21 · HR 150"},
                      {"name": "Sam", "session": sess("YOGA", "other", "19:00–20:00", "Studio"), "done": None}],
             "race": {"name": "City 10K", "days": 73, "date": "2026-11-29"},
             "quote": ("Those who think they have no time for exercise will sooner or later have to find time for illness.", "Edward Stanley")},
            {"rows": [{"name": "Solo", "session": sess("REST / MOBILITY", "rest", all_day=True), "done": None}],
             "race": {"name": "A Very Long Race Name Indeed 10K", "days": 5, "date": "x"},
             "quote": ("Wherever you are, be all there.", "Jim Elliot")},
        ]
        for c in cases:
            c.update({"date": str(DAY), "date_label": "FRI 18 SEP", "updated": "06:00"})
            img = r.render(c, fonts_dir="/nonexistent")  # falls back to the default font
            self.assertEqual(img.mode, "1")
            self.assertEqual(img.size, (800, 480))


class SpecialDays(unittest.TestCase):
    DAYS = [
        {"date": "09-22", "text": "Happy {nth} anniversary, A & S", "since": 2024, "icon": "heart"},
        {"date": "12-19", "text": "Happy birthday!", "author": "everyone", "icon": "cake"},
    ]

    def test_anniversary_counts_years(self):
        self.assertEqual(r.special_for(dt.date(2026, 9, 22), self.DAYS), ("Happy 2nd anniversary, A & S", "", "heart"))
        self.assertEqual(r.special_for(dt.date(2027, 9, 22), self.DAYS)[0], "Happy 3rd anniversary, A & S")
        self.assertEqual(r.special_for(dt.date(2035, 9, 22), self.DAYS)[0], "Happy 11th anniversary, A & S")

    def test_author_and_ordinary_days(self):
        self.assertEqual(r.special_for(dt.date(2026, 12, 19), self.DAYS), ("Happy birthday!", "everyone", "cake"))
        self.assertIsNone(r.special_for(dt.date(2026, 9, 21), self.DAYS))
        self.assertIsNone(r.special_for(dt.date(2026, 9, 22), None))

    def test_special_day_replaces_quote_in_build(self):
        cfg = {"timezone": TZ, "people": PEOPLE, "special_days": self.DAYS}
        data = r.build(dt.date(2026, 12, 19), [], cfg)
        self.assertEqual((data["quote"], data["special"], data["icon"]), (("Happy birthday!", "everyone"), True, "cake"))
        for day in (dt.date(2026, 9, 22), dt.date(2026, 12, 19)):  # heart, cake: both draw
            img = r.render(r.build(day, [], cfg), fonts_dir="/nonexistent")
            self.assertEqual(img.size, (800, 480))
        self.assertEqual(r.build(dt.date(2026, 9, 21), [], cfg)["special"], False)


if __name__ == "__main__":
    unittest.main()

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

    def test_whole_words_only(self):
        for label, kind in [
            ("Coffee with Sam", "other"), ("Office day", "other"), ("Forest walk", "other"), ("Brunch", "other"),
            ("Grace's party", "other"), ("Day off", "rest"), ("Rested legs", "rest"), ("Running club", "run"),
            ("Lifting", "strength"), ("Race day", "run"),
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
        self.assertEqual(rows, {"Solo": {"label": "YOGA", "kind": "other", "time": "19:00–20:00", "place": "",
                                         "all_day": False, "together": None}})


class Together(unittest.TestCase):
    """A shared outing: one band instead of two rows."""

    def sessions(self, events):
        rows = r.resolve(events, DAY, PEOPLE, TZ)
        return [rows["Alex"], rows["Sam"]]

    def test_tag_puts_one_event_on_both_rows(self):
        rows = r.resolve([ev("Long run 14 km #together", "06:30", "08:30", location="Riverside Park")], DAY, PEOPLE, TZ)
        self.assertEqual(rows["Alex"]["label"], "LONG RUN 14 KM")  # the tag never shows
        self.assertEqual(rows["Sam"]["label"], "LONG RUN 14 KM")
        self.assertTrue(rows["Alex"]["together"])
        t = r.togetherness([rows["Alex"], rows["Sam"]])
        self.assertEqual((t["time"], t["place"], t["same"], t["label"]), ("06:30–08:30", "Riverside Park", True, "LONG RUN 14 KM"))

    def test_group_prefixes(self):
        for title in ("Alex + Sam: Long run 14 km", "Alex & Sam: Long run 14 km", "Alex and Sam: Long run 14 km",
                      "Us: Long run 14 km", "Both: Long run 14 km", "Together: Long run 14 km"):
            rows = r.resolve([ev(title, "06:30", "08:30", location="Riverside Park")], DAY, PEOPLE, TZ)
            self.assertEqual(rows["Sam"]["label"], "LONG RUN 14 KM", title)
            self.assertIsNotNone(r.togetherness([rows["Alex"], rows["Sam"]]), title)

    def test_marker_holds_with_no_place_and_different_sessions(self):
        t = r.togetherness(self.sessions([
            ev("Long run 14 km #together", "06:30", "08:30"),
            ev("Sam: Easy run 6 km #together", "06:30", "07:30"),
        ]))
        self.assertEqual((t["same"], t["label"], t["place"]), (False, "", ""))

    def test_inferred_from_same_place_and_close_start(self):
        t = r.togetherness(self.sessions([
            ev("Long run 14 km", "06:30", "08:30", location="Riverside Park"),
            ev("Easy run 6 km", "06:45", "07:30", creator=PARTNER, location="riverside park "),
        ]))
        self.assertEqual((t["time"], t["place"], t["same"]), ("06:30", "Riverside Park", False))

    def test_not_together(self):
        apart = [
            # different places
            [ev("Threshold 3 × 10 min", "18:00", "19:30", location="Track"),
             ev("Yoga", "19:00", "20:00", creator=PARTNER, location="Studio")],
            # same place, too far apart in the day
            [ev("Easy run 7 km", "06:30", "07:30", location="Track"),
             ev("Yoga", "18:00", "19:00", creator=PARTNER, location="Track")],
            # same time, but neither event says where
            [ev("Strength · lower", "11:00", "12:00"),
             ev("Pilates", "11:00", "12:00", creator=PARTNER)],
            # would infer, but one of us called it off
            [ev("Easy run 7 km", "06:30", "07:30", location="Track"),
             ev("Yoga #apart", "06:30", "07:30", creator=PARTNER, location="Track")],
            # only one of us has anything on
            [ev("Long run 14 km", "06:30", "08:30", location="Riverside Park")],
        ]
        for events in apart:
            self.assertIsNone(r.togetherness(self.sessions(events)), events[0]["summary"])

    def test_window_of_zero_leaves_only_the_marker(self):
        inferred = self.sessions([ev("Long run 14 km", "06:30", "08:30", location="Park"),
                                  ev("Easy run 6 km", "06:45", "07:30", creator=PARTNER, location="Park")])
        self.assertIsNone(r.togetherness(inferred, window_min=0))
        marked = self.sessions([ev("Long run 14 km #together", "06:30", "08:30", location="Park")])
        self.assertIsNotNone(r.togetherness(marked, window_min=0))

    def test_needs_two_people(self):
        rows = r.resolve([ev("Long run 14 km #together", "06:30", "08:30")], DAY, [{"name": "Solo", "emails": []}], TZ)
        self.assertIsNone(r.togetherness([rows["Solo"]]))

    def test_build_collapses_only_when_the_done_state_matches(self):
        with tempfile.TemporaryDirectory() as t:
            log = Path(t) / "log.csv"
            log.write_text("date,type,name,distance_km,pace_min_km,avg_hr\n2026-09-18,run,Long,14.2,6:05,152\n")
            events = [ev("Long run 14 km #together", "06:30", "08:30", location="Riverside Park")]
            people = [{"name": "Alex", "emails": [ME], "activity_log": str(log)}, {"name": "Sam", "emails": [PARTNER]}]

            data = r.build(DAY, events, {"timezone": TZ, "people": people})
            self.assertEqual(data["together"]["same"], True)
            self.assertEqual(data["together"]["collapse"], False)  # Alex logged it, Sam has not

            both = [dict(p, activity_log=str(log)) for p in people]
            self.assertTrue(r.build(DAY, events, {"timezone": TZ, "people": both})["together"]["collapse"])
            neither = [{"name": p["name"], "emails": p["emails"]} for p in people]
            self.assertTrue(r.build(DAY, events, {"timezone": TZ, "people": neither})["together"]["collapse"])

    def test_window_is_configurable_and_absent_on_ordinary_days(self):
        cfg = {"timezone": TZ, "people": PEOPLE}
        events = [ev("Long run 14 km", "06:30", "08:30", location="Park"),
                  ev("Easy run 6 km", "07:30", "08:30", creator=PARTNER, location="Park")]
        self.assertIsNone(r.build(DAY, events, cfg)["together"])  # 60 min apart, default window is 30
        self.assertIsNotNone(r.build(DAY, events, dict(cfg, together_window_min=90))["together"])
        self.assertIsNone(r.build(DAY, [ev("Easy run 7 km", "18:00", "19:00")], cfg)["together"])

    def test_renders_both_together_layouts(self):
        base = {"date": str(DAY), "date_label": "SUN 21 SEP", "updated": "06:00",
                "race": {"name": "City 10K", "days": 64, "date": "2026-11-29"},
                "quote": ("Wherever you are, be all there.", "Jim Elliot")}
        sess = lambda label: {"label": label, "kind": "run", "time": "06:30", "place": "Riverside Park",  # noqa: E731
                              "all_day": False, "together": True}
        cases = [
            # collapsed: one label, both names under it
            dict(base, rows=[{"name": "Alex", "session": sess("LONG RUN 14 KM"), "done": None},
                             {"name": "Sam", "session": sess("LONG RUN 14 KM"), "done": None}],
                 together={"time": "06:30", "place": "Riverside Park", "same": True, "label": "LONG RUN 14 KM", "collapse": True}),
            # collapsed and done
            dict(base, rows=[{"name": "Alex", "session": sess("LONG RUN 14 KM"), "done": "14.2 KM · 6:05 · HR 152"},
                             {"name": "Sam", "session": sess("LONG RUN 14 KM"), "done": "14.2 KM · 6:05 · HR 152"}],
                 together={"time": "06:30", "place": "Riverside Park", "same": True, "label": "LONG RUN 14 KM", "collapse": True}),
            # same outing, our own work, one of us done
            dict(base, rows=[{"name": "Alex", "session": sess("LONG RUN 14 KM"), "done": "14.2 KM · 6:05 · HR 152"},
                             {"name": "Sam", "session": sess("EASY RUN 6 KM"), "done": None}],
                 together={"time": "06:30", "place": "Riverside Park", "same": False, "label": "", "collapse": False}),
            # a label long enough to have to shrink, and a band with nothing to say but the word
            dict(base, rows=[{"name": "Alex", "session": sess("LONG RUN 22 KM WITH 4 × 8 MIN AT MARATHON PACE"), "done": None},
                             {"name": "Sam", "session": sess("LONG RUN 22 KM WITH 4 × 8 MIN AT MARATHON PACE"), "done": None}],
                 together={"time": "", "place": "", "same": True,
                           "label": "LONG RUN 22 KM WITH 4 × 8 MIN AT MARATHON PACE", "collapse": True}),
        ]
        for c in cases:
            img = r.render(c, fonts_dir="/nonexistent")
            self.assertEqual((img.mode, img.size), ("1", (800, 480)))


class Emoji(unittest.TestCase):
    """Neither display face owns a symbol glyph, so anything Sam types has to fall back."""

    FONTS = Path("~/.local/state/training-display/fonts").expanduser()

    def font(self, name="BarlowCondensed-Bold.ttf", size=40):
        from PIL import ImageFont

        p = self.FONTS / name
        if not p.is_file():
            self.skipTest(f"{name} not installed — run scripts/fetch_fonts.sh")
        return ImageFont.truetype(str(p), size)

    def runs(self, text, with_emoji=True):
        text_font = self.font()
        emoji = self.font(r.EMOJI_FONT) if with_emoji else None
        if with_emoji and emoji is None:
            self.skipTest("no emoji font")
        return [(run, e) for run, _f, e in r._runs(text, text_font, emoji, {})]

    def test_glue_is_stripped_to_one_picture(self):
        for text, want in [
            ("❤️", "♥"),          # variation selector dropped, heart made solid
            ("\U0001F44D\U0001F3FD", "\U0001F44D"),  # skin tone dropped
            ("\U0001F3CB️‍♀️", "\U0001F3CB"),  # ZWJ sequence -> its first part
            ("1️⃣", "1"),              # keycap -> the digit
        ]:
            self.assertEqual("".join(run for run, _ in self.runs(text)), want, repr(text))

    def test_text_keeps_what_the_text_face_owns(self):
        # dashes, curly quotes and the bullet are ordinary text, not emoji
        self.assertEqual(self.runs("“Easy run 7–8 km” · ok"), [("“Easy run 7–8 km” · ok", False)])

    def test_emoji_run_is_split_out(self):
        self.assertEqual(self.runs("LONG RUN \U0001F525 TODAY"),
                         [("LONG RUN ", False), ("\U0001F525", True), (" TODAY", False)])

    def test_dropped_when_no_emoji_face(self):
        # no tofu, and no margin-wrecking space left where the picture was
        self.assertEqual(self.runs("\U0001F3CB PILATES \U0001F525", with_emoji=False), [("PILATES", False)])
        self.assertEqual(self.runs("\U0001F525", with_emoji=False), [])

    def test_glyph_cache_is_keyed_on_the_face_not_the_object(self):
        """`fit` builds and drops a font per size, so ids get recycled. Keyed on id(),
        the cache hands one face's answers to the next object at that address and the
        emoji comes out as .notdef — which is exactly what shipped to the wall once."""
        a, b = self.font(), self.font()
        self.assertIsNot(a, b)
        self.assertEqual(r._font_key(a), r._font_key(b))  # same face, same answers
        self.assertNotEqual(r._font_key(a), r._font_key(self.font(r.EMOJI_FONT)))
        self.assertNotEqual(r._font_key(a), r._font_key(self.font(size=20)))
        cache = {}
        self.assertTrue(r._has_glyph(self.font(r.EMOJI_FONT), "\U0001F525", cache))
        self.assertFalse(r._has_glyph(self.font(), "\U0001F525", cache))

    def test_renders_a_frame_with_emoji_everywhere(self):
        sess = {"label": "LONG RUN 14 KM \U0001F525", "kind": "run", "time": "06:30",
                "place": "Riverside Park ❤️", "all_day": False, "together": None}
        data = {"date": str(DAY), "date_label": "SUN 27 SEP", "updated": "06:00",
                "race": {"name": "City 10K \U0001F3C3", "days": 63, "date": "2026-11-29"},
                "quote": ("Wherever you are, be all there. ✨", "Jim Elliot"),
                "rows": [{"name": "Alex", "session": sess, "done": "14.2 KM · 6:05 · HR 152"},
                         {"name": "Sam", "session": None, "done": None}],
                "together": None}
        for fonts in (self.FONTS, "/nonexistent"):
            img = r.render(data, fonts_dir=fonts)
            self.assertEqual((img.mode, img.size), ("1", (800, 480)))


class Thai(unittest.TestCase):
    """Neither display face has Thai, and nothing reads the font's mark positioning."""

    FONTS = Emoji.FONTS
    font = Emoji.font
    THAI = "NotoSansThai-CondensedBold.ttf"

    def layout(self, text):
        f = self.font(self.THAI, 70)
        return f, r._thai_layout(text, f, {})

    def test_thai_run_goes_to_the_thai_face(self):
        text_font, thai = self.font(), self.font(self.THAI)
        runs = r._runs("YOGA IN พร้อมพงษ์", text_font, None, {}, thai)
        self.assertEqual([(run, f is thai) for run, f, _ in runs], [("YOGA IN ", False), ("พร้อมพงษ์", True)])

    def test_dropped_when_no_thai_face(self):
        runs = r._runs("YOGA IN พร้อมพงษ์", self.font(), None, {})
        self.assertEqual([run for run, _f, _e in runs], ["YOGA IN"])

    def test_marks_basic_layout_gets_right_are_left_alone(self):
        self.assertEqual(self.layout("พร้อมพงษ์")[1], ("พร้อมพงษ์", []))

    def test_tone_goes_over_the_upper_vowel(self):
        for text, over in [("ที่", "ี"), ("น้ำ", "ำ")]:
            f, (kept, moved) = self.layout(text)
            self.assertEqual(kept, text.replace(moved[0][1], ""), text)
            (_i, tone, dx, dy), = moved
            self.assertEqual(dx, 0)
            self.assertLess(r._ink(f, tone, {})[3] + dy, r._ink(f, over, {})[1], text)  # clear of it, above

    def test_marks_step_off_the_ascender(self):
        f, (kept, moved) = self.layout("ปั้น")
        self.assertEqual(kept, "ปน")
        self.assertEqual([m for _i, m, _x, _y in moved], ["ั", "้"])
        stem = r._ink(f, "ป", {}, top=r._ink(f, "บ", {})[1] - 1)[0]
        for _i, mark, dx, _dy in moved:
            self.assertLessEqual(f.getlength("ป") + r._ink(f, mark, {})[2] + dx, stem)
        self.assertLess(moved[1][3], 0)  # and the tone still climbs over the vowel

    def test_lower_vowel_drops_below_the_tail(self):
        f, (kept, moved) = self.layout("ฎุ")
        (_i, _m, dx, dy), = moved
        self.assertEqual((kept, dx), ("ฎ", 0))
        self.assertGreater(r._ink(f, "ุ", {})[1] + dy, r._ink(f, "ฎ", {})[3])

    def test_renders_a_frame_with_thai_everywhere(self):
        sess = {"label": "YOGA CLASS IN พร้อมพงษ์", "kind": "other", "time": "16:00",
                "place": "สวนลุมพินี", "all_day": False, "together": None}
        data = {"date": str(DAY), "date_label": "SUN 27 SEP", "updated": "06:00",
                "race": {"name": "วิ่งกรุงเทพ 10K", "days": 63, "date": "2026-11-29"},
                "quote": ("ไม่มีใครแก่เกินเรียน", "สุภาษิต"),
                "rows": [{"name": "Alex", "session": sess, "done": None},
                         {"name": "เบญญา", "session": dict(sess, label="ว่ายน้ำ ที่นี่"), "done": "ปั้น"}],
                "together": None}
        for fonts in (self.FONTS, "/nonexistent"):
            img = r.render(data, fonts_dir=fonts)
            self.assertEqual((img.mode, img.size), ("1", (800, 480)))


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


    def _long(self):
        sess = {"label": "PILATES REFORMER INTERMEDIATE CLASS WITH FRIENDS AND THEN SOME", "kind": "other",
                "time": "13:30–16:30", "place": "A Studio With A Very Long Name On The Far Side Of Town", "all_day": False}
        return {"date": str(DAY), "date_label": "FRI 18 SEP", "updated": "06:00", "together": None,
                "race": {"name": "An Extremely Long Race Name That Goes On And On 10K", "days": 5, "date": "x"},
                "rows": [{"name": "Alex", "session": sess, "done": "9.7 KM · 7:21 · HR 150 · AND A LOT MORE"},
                         {"name": "Sam", "session": dict(sess), "done": None}],
                "quote": (" ".join(["Keep going even when the road is long and the day is hot."] * 4), "Someone")}

    def test_nothing_runs_past_the_right_margin(self):
        img = r.render(self._long(), fonts_dir="/nonexistent")
        margin = [img.getpixel((x, y)) for x in range(r.W - 28 + 3, r.W) for y in range(r.H)]
        self.assertTrue(all(v == 255 for v in margin), "ink in the right margin")

    def test_fresh_frame_has_no_clock(self):
        a, b = self._long(), self._long()
        b["updated"] = "21:45"
        self.assertEqual(r.render(a, fonts_dir="/nonexistent").tobytes(), r.render(b, fonts_dir="/nonexistent").tobytes())
        b["stale"] = "last good 06:00"
        self.assertNotEqual(r.render(a, fonts_dir="/nonexistent").tobytes(), r.render(b, fonts_dir="/nonexistent").tobytes())


class Stale(unittest.TestCase):
    CFG = {"timezone": TZ, "people": PEOPLE}

    def test_same_day_keeps_the_last_good_frame(self):
        last = r.build(DAY, [ev("Easy run", "18:00", "19:30")], self.CFG, now=dt.datetime(2026, 9, 18, 9, 0))
        data = r.stale_frame(DAY, self.CFG, last)
        self.assertEqual(data["rows"][0]["session"]["label"], "EASY RUN")
        self.assertEqual(data["stale"], "last good 09:00")
        self.assertNotIn("offline", data)

    def test_new_day_never_shows_yesterday(self):
        last = r.build(DAY, [ev("Easy run", "18:00", "19:30")], self.CFG, now=dt.datetime(2026, 9, 18, 21, 0))
        data = r.stale_frame(DAY + dt.timedelta(days=1), self.CFG, last)
        self.assertEqual(data["date_label"], "SAT 19 SEP")
        self.assertIsNone(data["rows"][0]["session"])
        self.assertTrue(data["offline"])
        self.assertEqual(data["stale"], "last good FRI 18 SEP 21:00")
        self.assertEqual(r.render(data, fonts_dir="/nonexistent").size, (800, 480))

    def test_never_fetched(self):
        data = r.stale_frame(DAY, self.CFG, None)
        self.assertTrue(data["offline"])
        self.assertEqual(data["stale"], "calendar not reached yet")


class Battery(unittest.TestCase):
    def test_threshold_and_hysteresis(self):
        dev = lambda v: {"Battery-Voltage": v}  # noqa: E731 — the headers as server.py saves them
        self.assertFalse(r.battery_low(dev("4.01")))
        self.assertTrue(r.battery_low(dev("3.59")))
        self.assertTrue(r.battery_low(dev("3.65"), was_low=True))  # wobbling back over the line is not a charge
        self.assertFalse(r.battery_low(dev("3.72"), was_low=True))  # a charge is
        self.assertTrue(r.battery_low(dev("3.75"), low_v=3.8))

    def test_no_reading_keeps_the_last_answer(self):
        for dev in (None, {}, {"Battery-Voltage": "n/a"}):
            self.assertFalse(r.battery_low(dev))
            self.assertTrue(r.battery_low(dev, was_low=True))

    def test_mark_is_drawn_in_the_footer_and_inside_the_margin(self):
        base = Render()._long()
        low = dict(base, battery_low=True)
        a, b = r.render(base, fonts_dir="/nonexistent"), r.render(low, fonts_dir="/nonexistent")
        self.assertNotEqual(a.tobytes(), b.tobytes())
        self.assertEqual(a.crop((0, 0, r.W, r.H - 28)).tobytes(), b.crop((0, 0, r.W, r.H - 28)).tobytes())  # footer only
        both = r.render(dict(low, stale="last good 06:00"), fonts_dir="/nonexistent")
        self.assertTrue(all(both.getpixel((x, y)) == 255 for x in range(r.W - 28 + 3, r.W) for y in range(r.H)))


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

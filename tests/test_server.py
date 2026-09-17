"""Server tests: a real ThreadingHTTPServer on a loopback port with a temp output dir.

Run:  python -m unittest discover tests
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.opts = s.server_options({}, {"bind": "127.0.0.1", "port": 0})
        cls.httpd = s.serve(cls.out, cls.opts)
        cls.base = f"http://127.0.0.1:{cls.opts['port']}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None, method="GET", data=None):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def write_frame(self, content=b"\x89PNG fake frame 1"):
        (self.out / "today.png").write_bytes(content)
        (self.out / "today.json").write_text(json.dumps({"date": "2026-09-18"}))

    def write_real_frame(self, seed=0):
        from PIL import Image, ImageDraw

        im = Image.new("1", (800, 480), 1)
        ImageDraw.Draw(im).rectangle([10 + seed, 10, 200, 200], fill=0)
        im.save(self.out / "today.png")
        (self.out / "today.json").write_text("{}")

    def test_bmp_is_what_old_firmware_wants(self):
        self.write_real_frame()
        code, hdr, body = self.get("/today.bmp")
        self.assertEqual((code, hdr["Content-Type"]), (200, "image/bmp"))
        self.assertEqual(body[:2], b"BM")
        self.assertEqual(len(body), 48062)  # 62-byte header + 800*480/8, firmware 1.5.x checks this exactly
        # setup points at the bmp, display at the png
        self.assertTrue(json.loads(self.get("/api/setup", {"Host": "h:1"})[2])["image_url"].endswith("/today.bmp"))
        self.assertTrue(json.loads(self.get("/api/display", {"Host": "h:1"})[2])["image_url"].endswith("/today.png"))
        # cache follows the frame
        self.write_real_frame(seed=5)
        self.assertNotEqual(self.get("/today.bmp")[2], body)

    # ---- static
    def test_no_frame_yet(self):
        for p in (self.out / "today.png", self.out / "today.json"):
            p.unlink(missing_ok=True)
        self.assertEqual(self.get("/today.png")[0], 404)
        self.assertEqual(self.get("/today.json")[0], 404)
        code, _, body = self.get("/health")
        self.assertEqual(json.loads(body), {"ok": False, "age_s": None})
        # the device must not sleep an hour on nothing: status 500 -> fast retry
        code, _, body = self.get("/api/display")
        self.assertEqual(json.loads(body)["status"], 500)

    def test_png_json_page_health(self):
        self.write_frame()
        code, hdr, body = self.get("/today.png")
        self.assertEqual((code, hdr["Content-Type"]), (200, "image/png"))
        self.assertEqual(body, b"\x89PNG fake frame 1")
        self.assertEqual(hdr["Cache-Control"], "no-store")
        code, _, body = self.get("/today.json")
        self.assertEqual(json.loads(body)["date"], "2026-09-18")
        code, hdr, body = self.get("/")
        self.assertIn(b"/today.png?t=", body)
        self.assertTrue(hdr["Content-Type"].startswith("text/html"))
        self.assertTrue(json.loads(self.get("/health")[2])["ok"])
        self.assertEqual(self.get("/nope")[0], 404)

    # ---- TRMNL protocol
    def test_setup(self):
        code, _, body = self.get("/api/setup", {"ID": "AA:BB:CC:DD:EE:FF", "Host": "mini.local:8787"})
        d = json.loads(body)
        self.assertEqual(d["status"], 200)
        self.assertEqual(d["image_url"], "http://mini.local:8787/today.bmp")
        self.assertTrue(d["api_key"] and d["friendly_id"])
        seen = json.loads((self.out / "device.json").read_text())
        self.assertEqual(seen["ID"], "AA:BB:CC:DD:EE:FF")

    def test_display_filename_tracks_content(self):
        self.write_frame(b"frame A")
        d1 = json.loads(self.get("/api/display", {"Host": "10.0.0.5:8787"})[2])
        self.assertEqual(d1["status"], 0)
        self.assertEqual(d1["image_url"], "http://10.0.0.5:8787/today.png")
        self.assertEqual(d1["refresh_rate"], 3600)
        self.assertFalse(d1["update_firmware"] or d1["reset_firmware"])
        self.assertTrue(d1["filename"].endswith(".png"))
        self.assertLessEqual(len(d1["filename"]), 31)  # SPIFFS name limit in the firmware
        d2 = json.loads(self.get("/api/display")[2])
        self.assertEqual(d1["filename"], d2["filename"])  # same frame -> device skips the redraw
        self.write_frame(b"frame B")
        d3 = json.loads(self.get("/api/display")[2])
        self.assertNotEqual(d1["filename"], d3["filename"])

    def test_configured_image_url_wins(self):
        self.write_frame()
        self.opts["image_url"] = "http://192.168.1.2:8787/today.png"
        try:
            d = json.loads(self.get("/api/display", {"Host": "other:1"})[2])
            self.assertEqual(d["image_url"], "http://192.168.1.2:8787/today.png")
        finally:
            self.opts["image_url"] = None

    def test_log(self):
        code, _, _ = self.get("/api/log", {"ID": "x"}, method="POST", data=b'{"logs":[{"msg":"hi"}]}')
        self.assertEqual(code, 204)


class Options(unittest.TestCase):
    def test_config_then_cli(self):
        o = s.server_options({"server": {"port": 9000, "refresh_rate": 1800}}, {"port": None, "bind": "127.0.0.1"})
        self.assertEqual((o["port"], o["refresh_rate"], o["bind"]), (9000, 1800, "127.0.0.1"))
        self.assertEqual(s.server_options({})["port"], 8787)


if __name__ == "__main__":
    unittest.main()

"""截图：压缩、随会话采集、上传到服务端后查看和删除（不需要真机）"""
import io
import zipfile

from PIL import Image

from perfclient import screenshots
from perfclient.plugin import register

from test_plugins import FakePlatform, MemDevice, run_session


def png(w=1080, h=2340, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class ShotDevice(MemDevice):
    def screenshot(self):
        return png()


@register
class ShotPlatform(FakePlatform):
    name = "testplat"   # same platform, device that can take screenshots

    def device(self, serial):
        return ShotDevice(serial)


def test_shrink_to_small_jpeg():
    out = screenshots.shrink(png(), 360)
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and im.size == (360, 780)
    assert len(out) < 50_000


def test_parse_config():
    assert screenshots.parse_config({}) == {}
    assert screenshots.parse_config({"screenshots": None}) == {}
    assert screenshots.parse_config({"screenshots": True, "interval": 2}) == {"interval": 2.0, "width": 360}
    assert screenshots.parse_config({"screenshots": {"interval": 1, "width": 480}}) == {"interval": 1.0, "width": 480}


def test_session_records_screenshots(tmp_path):
    store, r = run_session(tmp_path, metrics=["answer"], screenshots={"interval": 0.2}, duration=0.9)
    shots = store.shots(r.id)
    assert len(shots) >= 3
    assert all(r.started_at - 1000 <= ts <= store.get_session(r.id)["ended_at"] for ts in shots)
    assert store.shot_path(r.id, shots[0]).read_bytes()[:2] == b"\xff\xd8"   # JPEG
    assert store.shots(r.id, since=shots[0]) == shots[1:]
    store.delete_session(r.id)
    assert store.shots(r.id) == []


def test_no_screenshots_unless_enabled(tmp_path):
    store, r = run_session(tmp_path, metrics=["answer"])
    assert store.shots(r.id) == []


def test_platform_without_screenshot_reports_once(tmp_path):
    register(FakePlatform)   # device without screenshot()
    try:
        store, r = run_session(tmp_path, metrics=["answer"], screenshots={"interval": 0.1})
    finally:
        register(ShotPlatform)
    errors = [e for e in store.events(r.id) if e[1] == "error"]
    assert [e[2] for e in errors] == ["这个平台不支持截图"]


def test_server_screenshots(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path / "blobs"))
    import gzip
    import importlib
    import json
    import perfserver.db
    import perfserver.main
    importlib.reload(perfserver.db); importlib.reload(perfserver.main)
    from fastapi.testclient import TestClient
    c = TestClient(perfserver.main.app)
    bundle = {"format": 2, "session": {"id": "s2", "name": "n", "project": "p", "tags": [],
              "device": {"platform": "android"}, "app_package": "app", "config": {},
              "started_at": 0, "ended_at": 3000, "status": "finished"},
              "metric_meta": {}, "points": [[0, "cpu", 1]], "events": []}
    assert c.post("/api/sessions/upload", files={"file": ("b.gz", gzip.compress(json.dumps(bundle).encode()))}).status_code == 200

    def batch(stamps):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for ts in stamps:
                z.writestr(f"{ts}.jpg", screenshots.shrink(png(), 360))
        return {"file": ("shots.zip", buf.getvalue(), "application/zip")}

    assert c.post("/api/sessions/s2/screenshots?reset=1", files=batch([1000, 2000])).json() == {"count": 2}
    assert c.post("/api/sessions/s2/screenshots?reset=0", files=batch([3000])).json() == {"count": 3}
    assert c.get("/api/sessions/s2/screenshots").json() == [1000, 2000, 3000]
    r = c.get("/api/sessions/s2/screenshots/2000")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    # re-upload starts over
    assert c.post("/api/sessions/s2/screenshots?reset=1", files=batch([5000])).json() == {"count": 1}
    assert c.get("/api/sessions/s2/screenshots/1000").status_code == 404
    assert c.post("/api/sessions/nope/screenshots", files=batch([1])).status_code == 404
    c.delete("/api/sessions/s2")
    assert c.get("/api/sessions/s2/screenshots").json() == []
    assert not list((tmp_path / "blobs").glob("shots/s2/*"))

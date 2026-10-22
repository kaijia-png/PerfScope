"""卡顿（Jank）、崩溃日志、冷启动（不需要真机）"""
import json

from perfclient.platforms.android.parsers import JankDetector, parse_crash_blocks, parse_framestats
from perfclient.platforms.ios.parsers import summarize_report
from perfclient import plugin
from perfclient.plugin import register
from perfclient.uploader import build_bundle

from test_plugins import FakePlatform, MemDevice, run_session

MS = 1_000_000


# ---------------------------------------------------------------- jank

def frames_csv(times_ms, start=0):
    rows = ["---PROFILEDATA---", "Flags,IntendedVsync,Vsync,FrameCompleted,"]
    for k, ft in enumerate(times_ms):
        iv = start + k * 16 * MS
        rows.append(f"0,{iv},{iv},{iv + int(ft * MS)},")
    rows.append(f"1,{start + 999 * MS},0,{start + 2000 * MS},")   # flagged frame: ignored
    return "\n".join(rows)


def test_parse_framestats():
    frames = parse_framestats("Total frames rendered: 10\n" + frames_csv([10, 12, 30]))
    assert [round(ft / MS) for _, ft in frames] == [10, 12, 30]


def test_jank_rules():
    d = JankDetector()
    # 3 normal frames, then: 90 ms (Jank), 130 ms right after (not 2x the new average -> no jank), ...
    jank, big, jank_ns, n = d.feed(parse_framestats(frames_csv([12, 12, 12, 90, 12, 12, 12, 130, 12, 12, 12, 70])))
    assert n == 12
    assert (jank, big) == (2, 1)            # 90 ms and 130 ms; 70 ms is under 83.3 ms
    assert round(jank_ns / MS) == 220


def test_jank_frames_not_counted_twice():
    d = JankDetector()
    text = frames_csv([12, 12, 12, 100])
    assert d.feed(parse_framestats(text))[0] == 1
    assert d.feed(parse_framestats(text)) == (0, 0, 0, 0)   # same window dumped again


# ---------------------------------------------------------------- crash logs

JAVA = """10-13 20:41:59.100  4321  4321 E AndroidRuntime: FATAL EXCEPTION: main
10-13 20:41:59.100  4321  4321 E AndroidRuntime: Process: ai.plaud.android.plaud, PID: 4321
10-13 20:41:59.100  4321  4321 E AndroidRuntime: java.lang.NullPointerException: Attempt to invoke virtual method
10-13 20:41:59.100  4321  4321 E AndroidRuntime: 	at ai.plaud.Recorder.start(Recorder.kt:42)"""
NATIVE = """10-13 20:42:10.000  5000  5000 F DEBUG   : pid: 4400, tid: 4410, name: Thread-7  >>> ai.plaud.android.plaud <<<
10-13 20:42:10.000  5000  5000 F DEBUG   : signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0
10-13 20:42:10.000  5000  5000 F DEBUG   : Abort message: 'boom'"""
OTHER = """10-13 20:43:00.000  6000  6000 E AndroidRuntime: FATAL EXCEPTION: main
10-13 20:43:00.000  6000  6000 E AndroidRuntime: Process: com.other.app, PID: 6000"""


def test_android_crash_blocks():
    found = parse_crash_blocks((JAVA + "\n" + NATIVE + "\n" + OTHER).splitlines(), "ai.plaud.android.plaud")
    assert [c["title"] for c in found] == [
        "闪退：java.lang.NullPointerException: Attempt to invoke virtual method",
        "Native 闪退：signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 · Abort message: 'boom'"]
    assert "Recorder.kt:42" in found[0]["text"]


def ips(header, body):
    return json.dumps(header) + "\n" + json.dumps(body)


def test_ios_crash_summary():
    crash = ips({"bug_type": "309", "name": "Plaud-Global"},
                {"exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"}, "faultingThread": 0,
                 "threads": [{"frames": [{"imageIndex": 0, "symbol": "-[Recorder start]"}]}],
                 "usedImages": [{"name": "Plaud-Global"}]})
    assert summarize_report("/Plaud-Global-2026-10-13-200000.ips", crash, "Plaud-Global") == {
        "kind": "crash", "title": "闪退：EXC_BAD_ACCESS (SIGSEGV) · Plaud-Global -[Recorder start]"}
    fault = ips({"bug_type": "385", "name": "Plaud-Global"}, {"exception": {"type": "EXC_GUARD"}})
    assert summarize_report("/ExcUserFault_Plaud-Global-2026-10-13-1.ips", fault, "Plaud-Global")["kind"] == "fault"
    assert summarize_report("/Other-2026-10-13-1.ips", crash, "Plaud-Global") is None


def test_ios_jetsam_summary():
    j = ips({"bug_type": "298"}, {"pageSize": 16384, "processes": [
        {"name": "Plaud-Global", "reason": "per-process-limit", "rpages": 128000}, {"name": "Other"}]})
    assert summarize_report("/JetsamEvent-2026-10-13.ips", j, "Plaud-Global") == {
        "kind": "jetsam", "title": "内存超限被系统终止（per-process-limit），当时占用约 2000 MB"}
    assert summarize_report("/JetsamEvent-2026-10-13.ips", j, "Somebody") is None   # someone else was killed


# ---------------------------------------------------------------- session flow

class CrashyDevice(MemDevice):
    calls = 0

    def crashes(self, app, state=None):
        CrashyDevice.calls += 1
        if state is None:
            return [], {"old"}
        return [{"id": "c1", "kind": "crash", "title": "闪退：Boom", "text": "stack..."}], state | {"c1"}

    def cold_start(self, app):
        return 512.0


class CrashyPlatform(FakePlatform):
    name = "testplat"

    def device(self, serial):
        return CrashyDevice(serial)


def test_session_collects_crash_log_and_cold_start(tmp_path):
    prev = plugin._REG["platform"]["testplat"]   # other test modules register their own testplat
    register(CrashyPlatform)
    try:
        store, r = run_session(tmp_path, metrics=["answer"], launch={"cold": 2}, duration=0.3)
    finally:
        register(prev)
    evs = store.events(r.id)
    crashes = [e for e in evs if e[1] == "crash"]
    assert len(crashes) == 1 and crashes[0][2] == "闪退：Boom"          # found by the final sweep, once
    atts = store.attachments(r.id)
    assert atts == [{"ts": crashes[0][0], "kind": "crash", "title": "闪退：Boom", "text": "stack..."}]
    assert [p[2] for p in store.points(r.id) if p[1] == "launch_ms"] == [512.0, 512.0]
    assert sum(1 for e in evs if e[1] == "launch") == 2
    bundle = build_bundle(store, r.id)
    assert bundle["attachments"][0]["title"] == "闪退：Boom"
    assert bundle["metric_meta"]["launch_ms"]["label"] == "冷启动耗时"


def test_platform_without_crash_support(tmp_path):
    prev = plugin._REG["platform"]["testplat"]
    register(FakePlatform)                                    # MemDevice: no crashes(), no error event
    try:
        store, r = run_session(tmp_path, metrics=["answer"])
    finally:
        register(prev)
    assert not [e for e in store.events(r.id) if e[1] == "error" and "崩溃" in e[2]]


def test_server_keeps_crash_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path / "blobs"))
    import gzip
    import importlib
    import perfserver.db
    import perfserver.main
    importlib.reload(perfserver.db); importlib.reload(perfserver.main)
    from fastapi.testclient import TestClient
    c = TestClient(perfserver.main.app)
    att = [{"ts": 500, "kind": "crash", "title": "闪退：Boom", "text": "stack..."}]
    bundle = {"format": 2, "session": {"id": "s3", "name": "n", "project": "p", "tags": [],
              "device": {"platform": "android"}, "app_package": "app", "config": {},
              "started_at": 0, "ended_at": 1000, "status": "finished"},
              "metric_meta": {}, "points": [[0, "cpu", 1]], "events": [[500, "crash", "闪退：Boom"]], "attachments": att}
    assert c.post("/api/sessions/upload", files={"file": ("b.gz", gzip.compress(json.dumps(bundle).encode()))}).status_code == 200
    assert c.get("/api/sessions/s3/attachments").json() == att
    assert c.get("/api/sessions/s3").json()["crash_count"] == 1
    c.delete("/api/sessions/s3")
    assert c.get("/api/sessions/s3/attachments").json() == []


def test_client_download(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path / "blobs"))
    import importlib
    import io
    import zipfile
    import perfserver.db
    import perfserver.main
    importlib.reload(perfserver.db); importlib.reload(perfserver.main)
    from fastapi.testclient import TestClient
    c = TestClient(perfserver.main.app, base_url="http://10.1.2.3:9000")
    r = c.get("/download/PerfScope.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert z.read("PerfScope/server.txt").decode().strip() == "http://10.1.2.3:9000"   # the address they used
    assert "http://10.1.2.3:9000" in z.read("PerfScope/START-HERE.txt").decode()
    for f in ("start.sh", "PerfScope.command"):                                       # still executable
        assert (z.getinfo(f"PerfScope/{f}").external_attr >> 16) & 0o111
    assert "PerfScope/client/perfclient/app.py" in names
    assert not [n for n in names if any(x in n for x in ("/.venv/", "/data/", "__pycache__", ".db", "run.log"))]


# ---------------------------------------------------------------- page load time

def test_page_load_from_app_log():
    from perfclient.logrules import DEFAULT_MARK_RULE, compile_rules, match_line
    import re
    rules, mark = compile_rules(None), re.compile(DEFAULT_MARK_RULE)
    assert match_line("I/Recorder( 1): PERF_PAGE|录音详情|420", rules, mark) == ("metric", "page.录音详情", 420.0)
    assert match_line("PERF_PAGE|Agent 对话|780.5", rules, mark) == ("metric", "page.Agent 对话", 780.5)
    assert match_line("PERF|asr_latency|300", rules, mark) == ("metric", "biz.asr_latency", 300.0)   # unchanged


def test_page_load_from_android_displayed():
    from perfclient.platforms.android.parsers import parse_displayed
    app = "ai.plaud.android.plaud"
    assert parse_displayed(f"I/ActivityTaskManager( 1000): Displayed {app}/.MainActivity for user 0: +1s234ms", app) == ("MainActivity", 1234)
    assert parse_displayed(f"I/ActivityManager( 900): Displayed {app}/ai.plaud.ui.DetailActivity: +412ms", app) == ("DetailActivity", 412)
    assert parse_displayed(f"I/ActivityTaskManager( 1000): Fully drawn {app}/.MainActivity: +2s5ms", app) == ("MainActivity（完全绘制）", 2005)
    assert parse_displayed("I/ActivityTaskManager( 1000): Displayed com.other/.Main: +100ms", app) is None


def test_page_metric_meta():
    from perfclient import plugin
    assert plugin.metric_meta("page.录音详情", "android") == {"label": "页面加载：录音详情", "unit": "ms", "better": "lower", "order": 900}

"""The extension mechanism, tested end to end with a platform that needs no device."""
import time

from perfclient import plugin
from perfclient.plugin import Collector, Device, Hook, MetricDef, Platform, Source, register
from perfclient.session import SessionRunner
from perfclient.storage import Store
from perfclient.uploader import build_bundle

CALLS = {"start": 0, "end": 0}


class MemDevice(Device):
    platform = "testplat"

    def info(self):
        return {"platform": "testplat", "serial": self.serial, "model": "Unit"}

    def pid(self, app):
        return 42

    def read_files(self, paths):
        return {p: "7" for p in paths}


@register
class FakePlatform(Platform):
    name = "testplat"

    def list_devices(self):
        return [{"serial": "T1", "state": "device", "model": "Unit"}]

    def device(self, serial):
        return MemDevice(serial)


@register
class GoodCollector(Collector):
    name = "test.good"
    platforms = ("testplat",)
    metrics = [MetricDef("answer", "答案", "个", "higher", 1),
               MetricDef("other", "未启用的指标", "", "lower", 2)]

    def files(self, pid):
        return [f"/proc/{pid}/x"]

    def sample(self, tick):
        return {"answer": int(tick.files[f"/proc/{tick.pid}/x"]) * 6, "other": 1}


@register
class BrokenCollector(Collector):
    name = "test.broken"
    platforms = ("testplat",)
    metrics = [MetricDef("broken", "坏掉的指标")]

    def sample(self, tick):
        raise RuntimeError("boom")


@register
class FakeSource(Source):
    name = "test.source"
    platforms = ("testplat",)

    def start(self):
        self.emit_metric(int(time.time() * 1000), "biz.latency", 123.0)
        self.emit_event(int(time.time() * 1000), "marker", "来自数据源")


@register
class CountingHook(Hook):
    name = "test.hook"

    def on_session_start(self, session):
        CALLS["start"] += 1

    def on_session_end(self, session, store):
        CALLS["end"] += 1


def run_session(tmp_path, **cfg):
    store = Store(tmp_path / "t.db")
    config = {"target": {"platform": "testplat", "serial": "T1", "package": "app"},
              "interval": 0.1, "duration": 0.5, **cfg}
    r = SessionRunner(store, config)
    r.start()
    r.wait()
    r.stop()
    return store, r


def test_metric_defs_ordered_and_scoped():
    defs = plugin.metric_defs("testplat")
    assert list(defs)[:2] == ["answer", "other"]
    assert "cpu" not in defs  # android metrics do not leak into other platforms


def test_session_with_plugins(tmp_path):
    store, r = run_session(tmp_path, metrics=["answer", "broken"],
                           metric_meta={"biz.latency": {"label": "延迟", "unit": "ms"}})
    pts = store.points(r.id)
    keys = {p[1] for p in pts}
    assert "answer" in keys and all(p[2] == 42 for p in pts if p[1] == "answer")
    assert "other" not in keys            # produced but not enabled -> dropped
    assert "biz.latency" in keys          # from the source
    evs = store.events(r.id)
    errors = [e for e in evs if e[1] == "error"]
    assert len(errors) == 1 and "test.broken" in errors[0][2]  # reported once, others unaffected
    assert any(e[1] == "marker" and e[2] == "来自数据源" for e in evs)
    assert CALLS["start"] >= 1 and CALLS["end"] >= 1

    bundle = build_bundle(store, r.id)
    assert bundle["metric_meta"]["answer"]["label"] == "答案"
    assert bundle["metric_meta"]["biz.latency"] == {"label": "延迟", "unit": "ms",
                                                   "better": "lower", "order": 1000}


def test_unknown_metric_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="nope"):
        run_session(tmp_path, metrics=["nope"])


def test_override_builtin_by_name():
    @register
    class GoodCollector2(GoodCollector):
        name = "test.good"  # same name replaces the earlier registration
        metrics = [MetricDef("answer", "新答案", "个", "higher", 1)]
    assert plugin.metric_defs("testplat")["answer"].label == "新答案"
    register(GoodCollector)  # restore


def test_server_stats_and_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path / "blobs"))
    import importlib, gzip, json
    import perfserver.db, perfserver.main
    importlib.reload(perfserver.db); importlib.reload(perfserver.main)
    from fastapi.testclient import TestClient
    c = TestClient(perfserver.main.app)
    bundle = {"format": 2, "session": {"id": "s1", "name": "n", "project": "p", "tags": [],
              "device": {"platform": "testplat"}, "app_package": "app", "config": {},
              "started_at": 0, "ended_at": 1000, "status": "finished"},
              "metric_meta": {"threads": {"label": "线程数", "unit": "个", "better": "lower", "order": 25}},
              "points": [[0, "threads", 10], [500, "threads", 12]], "events": []}
    r = c.post("/api/sessions/upload", files={"file": ("b.gz", gzip.compress(json.dumps(bundle).encode()))})
    assert r.status_code == 200
    d = c.get("/api/sessions/s1").json()
    assert d["metric_meta"]["threads"]["label"] == "线程数"   # server never heard of "threads"
    assert d["stats"]["threads"]["max"] == 12
    assert c.get("/api/metrics").json()["metrics"] == ["threads"]

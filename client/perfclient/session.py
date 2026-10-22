"""Runs one test session by orchestrating whatever plugins are installed.

The runner knows nothing about specific metrics or platforms: it asks the platform
for a device, asks collectors which files they need, reads them in one batch,
lets each collector compute its values, and records everything.
"""
import re
import threading
import time
import uuid

from . import plugin, screenshots
from .plugin import SessionContext, Tick

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


def parse_alerts(alerts: dict):
    """{"mem_pss": "> 800"} -> {"mem_pss": (op, 800.0, text)}"""
    out = {}
    for metric, expr in (alerts or {}).items():
        m = re.fullmatch(r"\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*", str(expr))
        if not m:
            raise ValueError(f"告警条件格式不对: {metric}: {expr}（示例: \"> 800\"）")
        out[metric] = (m.group(1), float(m.group(2)), str(expr).strip())
    return out


def parse_duration(v):
    """'2h' / '30m' / '90s' / 120 -> seconds (None = run until stopped)."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hms]?)\s*", str(v))
    if not m:
        raise ValueError(f"时长格式不对: {v}（示例: 2h / 30m / 90s）")
    return float(m.group(1)) * {"h": 3600, "m": 60, "s": 1, "": 1}[m.group(2)]


def enabled_sources(platform_name, cfg):
    """config `sources: [names]` selects explicitly; otherwise the defaults.
    `log_channel: false` (older config key) turns off the app log source (logcat / iOS syslog)."""
    available = plugin.sources(platform_name)
    if cfg.get("sources") is not None:
        wanted = set(cfg["sources"])
        unknown = wanted - {s.name for s in available}
        if unknown:
            raise ValueError(f"未安装的数据源: {', '.join(sorted(unknown))}")
        return [s for s in available if s.name in wanted]
    chosen = [s for s in available if s.default_enabled]
    if cfg.get("log_channel") is False:
        chosen = [s for s in chosen if not s.name.endswith((".logcat", ".syslog"))]
    return chosen


class SessionRunner:
    def __init__(self, store, config: dict):
        plugin.load_plugins()
        self.store = store
        self.cfg = config
        target = config["target"]
        self.platform_name = target.get("platform", "android")
        self.platform = plugin.get_platform(self.platform_name)
        self.device = self.platform.device(target["serial"])
        self.app = target.get("package") or target.get("app")
        if not self.app:
            raise ValueError("请指定被测应用（target.package）")
        self.interval = float(config.get("interval", 1.0))
        self.duration = parse_duration(config.get("duration"))
        self.alerts = parse_alerts(config.get("alerts"))
        self._alert_state = {}

        defs = plugin.metric_defs(self.platform_name)
        self.metrics = list(config.get("metrics") or defs.keys())
        unknown = [m for m in self.metrics if m not in defs]
        if unknown:
            raise ValueError(f"没有采集器提供这些指标: {', '.join(unknown)}")
        # an alert on a metric this platform never produces would silently never fire
        bad = [m for m in self.alerts if m not in defs and not m.startswith((plugin.BIZ_PREFIX, "page."))]
        if bad:
            raise ValueError(f"告警条件里的指标 {', '.join(bad)} 在 {self.platform.label or self.platform_name} "
                             f"上不存在，可用的有: {', '.join(defs)}（业务指标以 biz. 开头）")

        self.id = uuid.uuid4().hex
        self.ctx = SessionContext(self.id, self.device, self.app, config)
        owners = plugin.metric_owners(self.platform_name)
        groups = {}
        for m in self.metrics:
            groups.setdefault(owners[m], set()).add(m)
        self.collectors = [cls(self.ctx, ms) for cls, ms in groups.items()]
        self.sources = [cls(self.ctx, self._on_source_metric, self._on_source_event)
                        for cls in enabled_sources(self.platform_name, config)]
        self.hooks = [cls() for cls in plugin.hooks()]
        # cold start measured before sampling: config `launch: {cold: 3}`
        self.cold_runs = int(((config.get("launch") or {}).get("cold") or 0))
        if self.cold_runs:
            config.setdefault("metric_meta", {}).setdefault(
                "launch_ms", {"label": "冷启动耗时", "unit": "ms", "better": "lower", "order": 5})
        shot_cfg = screenshots.parse_config(config)
        self.recorder = screenshots.ScreenshotRecorder(
            self.device, store, self.id, shot_cfg["interval"], shot_cfg["width"],
            lambda msg: self._event("error", msg)) if shot_cfg else None

        self.pid = None
        self._dead_reported = False
        self._failed = set()
        self.started_at = None
        self._stop = threading.Event()
        self._thread = None
        self._finish_lock = threading.Lock()
        self.status = "created"
        self.last_error = None

    # ---------------------------------------------------------------- lifecycle
    def session_meta(self):
        return self.store.get_session(self.id)

    def start(self):
        info = self.device.info()
        self.started_at = int(time.time() * 1000)
        cfg = {k: v for k, v in self.cfg.items() if k != "target"}
        cfg["platform"] = self.platform_name
        self.store.create_session({
            "id": self.id,
            "name": self.cfg.get("name") or f"{self.app} {time.strftime('%m-%d %H:%M')}",
            "project": self.cfg.get("project", "default"),
            "tags": self.cfg.get("tags", []),
            "notes": self.cfg.get("notes", ""),
            "device": info,
            "app_package": self.app,
            "app_version": self.device.app_version(self.app),
            "config": cfg,
            "started_at": self.started_at,
        })
        self.status = "running"
        for s in self.sources:
            try:
                s.start()
            except Exception as e:
                self._event("error", f"数据源 {s.name} 启动失败: {e}")
        if self.recorder:
            self.recorder.start()
        self._crash_baseline()
        self._run_hooks("on_session_start", self.session_meta())
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, status="finished"):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.interval * 3 + 30)
        self._finish(status)

    def wait(self):
        while self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _finish(self, status):
        with self._finish_lock:
            if self.status != "running":
                return
            self.status = status
        if self.recorder:
            self.recorder.stop()
        self._check_crashes()   # last sweep: reports written after the final restart, non-fatal faults
        for s in self.sources:
            try:
                s.stop()
            except Exception:
                pass
        self.store.finish_session(self.id, int(time.time() * 1000), status)
        self._run_hooks("on_session_end", self.session_meta(), self.store)

    # ---------------------------------------------------------------- crash reports
    # Platforms keep crash logs (Android crash buffer, iOS CrashReporter). We note what exists at the
    # start, then collect anything new for the app after it dies/restarts and once more at the end.
    def _crash_baseline(self):
        self._crash_state, self._crash_seen = None, set()
        self._crash_lock = threading.Lock()
        try:
            _, self._crash_state = self.device.crashes(self.app, None)
            self._crash_supported = True
        except NotImplementedError:
            self._crash_supported = False
        except Exception as e:
            self._crash_supported = True   # try again later; the device may just be slow to answer
            self._event("error", f"读取崩溃日志失败: {e}")

    def _check_crashes(self):
        if not getattr(self, "_crash_supported", False):
            return
        with self._crash_lock:
            try:
                found, self._crash_state = self.device.crashes(self.app, self._crash_state)
            except Exception as e:
                self._event("error", f"读取崩溃日志失败: {e}")
                return
            for c in found:
                if c["id"] in self._crash_seen:
                    continue
                self._crash_seen.add(c["id"])
                ts = int(time.time() * 1000)
                self.store.add_attachment(self.id, ts, c["kind"], c["title"], c["text"])
                self._event(c["kind"], c["title"], ts)

    def _crash_check_later(self):
        """Crash reports land a few seconds after the process dies; look twice."""
        def run():
            for delay in (3, 12):
                time.sleep(delay)
                if self.status != "running":
                    return   # the final sweep in _finish covers it
                self._check_crashes()
        threading.Thread(target=run, daemon=True).start()

    def _run_hooks(self, method, *args):
        for h in self.hooks:
            try:
                getattr(h, method)(*args)
            except Exception as e:
                self._event("error", f"钩子 {h.name} 执行失败: {e}")

    # ---------------------------------------------------------------- sampling
    def _measure_cold_start(self):
        for k in range(self.cold_runs):
            if self._stop.is_set():
                return
            try:
                ms = self.device.cold_start(self.app)
            except NotImplementedError:
                self._event("error", f"{self.platform.label or self.platform_name} 暂不支持自动测冷启动，"
                                     "可以让 App 打日志 PERF|launch_ms|耗时 上报")
                return
            except Exception as e:
                self._event("error", f"冷启动测量失败: {e}")
                return
            ts = int(time.time() * 1000)
            self.store.add_points(self.id, ts, {"launch_ms": ms})
            self._event("launch", f"冷启动 {ms:.0f} ms（第 {k + 1}/{self.cold_runs} 次）", ts)
            self._stop.wait(3)   # let the app settle before the next run / before sampling

    def _loop(self):
        if self.cold_runs:
            self._measure_cold_start()
        t_start = time.time()
        while not self._stop.is_set():
            tick_start = time.time()
            if self.duration and tick_start - t_start >= self.duration:
                break
            try:
                self._tick()
                self.last_error = None
            except Exception as e:  # device unplugged etc.: record and keep trying
                if self.last_error != str(e):
                    self._event("error", f"采集异常: {e}")
                self.last_error = str(e)
            self._stop.wait(max(0.0, self.interval - (time.time() - tick_start)))
        self._finish("finished")

    def _check_process(self):
        if not any(c.needs_process for c in self.collectors):
            return
        pid = self.device.pid(self.app)
        if pid is None:
            if not self._dead_reported:
                self._event("process_dead", f"{self.app} 未运行")
                self._dead_reported = True
                if self.pid is not None:   # it was running and went away
                    self._crash_check_later()
        elif self.pid is not None and pid != self.pid:
            self._event("process_restart", f"进程重启 pid {self.pid} → {pid}")
            self._crash_check_later()
            for c in self.collectors:
                c.reset()
        elif self._dead_reported:
            self._event("process_start", f"进程启动 pid {pid}")
            for c in self.collectors:
                c.reset()
        if pid is not None:
            self._dead_reported = False
        self.pid = pid

    def _tick(self):
        self._check_process()
        active = [c for c in self.collectors if not (c.needs_process and self.pid is None)]
        paths = []
        for c in active:
            try:
                for p in c.files(self.pid):
                    if p not in paths:
                        paths.append(p)
            except Exception:
                pass
        files = self.device.read_files(paths) if paths else {}
        tick = Tick(ts=int(time.time() * 1000), pid=self.pid, files=files)
        values = {}
        for c in active:
            try:
                out = c.sample(tick) or {}
                values.update({k: v for k, v in out.items() if k in c.enabled and v is not None})
                self._failed.discard(c.name)
            except Exception as e:
                if c.name not in self._failed:  # report once until it recovers
                    self._failed.add(c.name)
                    self._event("error", f"采集器 {c.name} 出错: {e}")
        self.store.add_points(self.id, tick.ts, values)
        self._check_alerts(tick.ts, values)

    # ---------------------------------------------------------------- inputs
    def _event(self, etype, label, ts=None):
        self.store.add_event(self.id, ts or int(time.time() * 1000), etype, label)

    def _on_source_metric(self, ts, key, value):
        if self.status != "running":
            return
        self.store.add_points(self.id, ts, {key: value})
        self._check_alerts(ts, {key: value})

    def _on_source_event(self, ts, etype, label):
        if self.status == "running":
            self._event(etype, label, ts)

    def add_marker(self, label, ts=None):
        """ts: when the tester acted (clicked, or started typing), so typing the name doesn't shift it"""
        now = int(time.time() * 1000)
        ts = now if ts is None else max(self.started_at or 0, min(int(ts), now))
        self._event("marker", label, ts)
        return ts

    def _check_alerts(self, ts, values):
        for metric, v in values.items():
            rule = self.alerts.get(metric)
            if not rule:
                continue
            op, thr, text = rule
            hit = OPS[op](v, thr)
            if hit and not self._alert_state.get(metric):  # only on entering the state
                self._event("alert", f"{metric} = {v} 触发 {text}", ts)
            self._alert_state[metric] = hit

    def snapshot(self):
        return {"id": self.id, "status": self.status, "platform": self.platform_name,
                "package": self.app, "serial": self.device.serial,
                "started_at": self.started_at, "duration": self.duration,
                "last_error": self.last_error}

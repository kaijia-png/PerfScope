"""Built-in iOS platform (real devices over USB), on top of pymobiledevice3.

Like Android it is written entirely against the public plugin API. Differences worth knowing:
  - Instruments data (CPU / memory / threads / FPS / per-app traffic) needs Developer Mode on
    the iPhone; the Developer Disk Image is mounted automatically. iOS 17+ goes through a
    userspace tunnel, so no sudo / tunneld is needed (except iOS 17.0-17.3 off macOS).
  - There are no files to read: collectors pull the latest value from streams kept by IOSAgent.
"""
import asyncio
import threading
import time

from ...logrules import DEFAULT_MARK_RULE, compile_rules, match_line
from ...plugin import Collector, Device, MetricDef, Platform, RateTracker, Source, register
from .agent import (IOSAgent, IOSError, fps_stream, friendly_error, net_stream, syslog_lines,
                    sysmon_stream)
from .parsers import battery_values, net_counters, proc_values, summarize_report


class IOSDevice(Device):
    platform = "ios"

    def __init__(self, serial):
        super().__init__(serial)
        self.agent = IOSAgent.get(serial)
        self._apps = None

    def _call(self, coro, timeout=30):
        try:
            return self.agent.call(coro, timeout)
        except IOSError:
            raise
        except Exception as e:
            # connection-level failure (unplugged, locked, rebooted): start fresh next time
            try:
                self.agent.call(self.agent.reset(), timeout=10)
            except Exception:
                pass
            raise IOSError(friendly_error(e))

    def info(self):
        return self._call(self.agent.info())

    def _app_infos(self, refresh=False):
        if self._apps is None or refresh:
            self._apps = self._call(self.agent.apps(), timeout=60)
        return self._apps

    def list_apps(self, third_party=True):
        return sorted(self._app_infos(refresh=True))

    def app_version(self, app):
        try:
            a = self._app_infos().get(app) or {}
        except IOSError:
            return ""
        ver, build = a.get("CFBundleShortVersionString", ""), a.get("CFBundleVersion", "")
        return f"{ver} ({build})" if ver and build and build != ver else ver or build

    def executable(self, app):
        """Process name of an app, used to pick its lines out of the system log."""
        a = (self._app_infos() or {}).get(app) or {}
        return a.get("CFBundleExecutable") or app.rsplit(".", 1)[-1]

    def screenshot(self):
        return self._call(self.agent.screenshot(), timeout=30)

    def crashes(self, app, state=None):
        names, texts = self._call(self.agent.crash_reports(state), timeout=60)
        if state is None:
            return [], names
        proc = self.executable(app)
        found = []
        for name, text in texts.items():
            s = summarize_report(name, text, proc)
            if s:
                found.append({"id": f"ios:{name}", "kind": s["kind"], "title": s["title"], "text": text})
        return found, names

    def pid(self, app):
        # the first call also brings up the tunnel and mounts the developer image: allow time
        return self._call(self.agent.pid(app), timeout=90)

    def latest(self, name, factory, *args):
        try:
            return self.agent.latest(name, factory, *args)
        except IOSError:
            raise
        except Exception as e:
            raise IOSError(friendly_error(e))


@register
class IOSPlatform(Platform):
    name = "ios"
    label = "iOS"

    def list_devices(self):
        from .agent import _lib
        _lib()
        from pymobiledevice3.usbmux import list_devices
        try:
            muxed = asyncio.run(list_devices())
        except Exception as e:
            if type(e).__name__ == "ConnectionFailedToUsbmuxdError":
                return []   # no Apple device service on this machine: simply no iOS devices
            raise IOSError(friendly_error(e))
        seen, out = set(), []
        # USB first, so a device also visible over Wi-Fi is listed once, as USB
        for d in sorted(muxed, key=lambda d: not d.is_usb):
            if d.serial in seen:
                continue
            seen.add(d.serial)
            entry = {"serial": d.serial, "state": "device", "model": "", "platform": "ios"}
            try:
                info = IOSAgent.get(d.serial).call(IOSAgent.get(d.serial).info(), timeout=10)
                entry["model"] = info.get("name") or info.get("model")
            except Exception as e:
                entry["state"] = "unauthorized"
                entry["error"] = str(e) if isinstance(e, IOSError) else friendly_error(e)
            out.append(entry)
        return out

    def device(self, serial):
        return IOSDevice(serial)


# ---------------------------------------------------------------- collectors

@register
class IOSSysmonCollector(Collector):
    name = "ios.sysmon"
    platforms = ("ios",)
    metrics = [MetricDef("cpu", "App CPU", "%", "lower", 10),
               MetricDef("mem_footprint", "App 内存 (Footprint)", "MB", "lower", 20),
               MetricDef("threads", "线程数", "个", "lower", 25)]

    def sample(self, tick):
        dev = self.ctx.device
        procs = dev.latest("sysmon", sysmon_stream)
        if not procs or tick.pid not in procs:
            return {}
        return proc_values(procs[tick.pid], dev.agent.ncpu)


@register
class IOSFpsCollector(Collector):
    name = "ios.fps"
    platforms = ("ios",)
    metrics = [MetricDef("fps", "FPS(整屏)", "帧/秒", "higher", 30)]

    def sample(self, tick):
        v = self.ctx.device.latest("fps", fps_stream)
        return {"fps": v} if isinstance(v, (int, float)) else {}


@register
class IOSNetCollector(Collector):
    name = "ios.net"
    platforms = ("ios",)
    metrics = [MetricDef("net_rx", "下行流量(App)", "KB/s", "lower", 40),
               MetricDef("net_tx", "上行流量(App)", "KB/s", "lower", 41)]

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.rates = RateTracker()

    def sample(self, tick):
        rec = self.ctx.device.latest(f"net:{tick.pid}", net_stream, tick.pid)
        if not rec:
            return {}
        out = {}
        for key, val in zip(("net_rx", "net_tx"), net_counters(rec)):
            if val is not None:
                r = self.rates.rate(key, val, tick.ts)
                if r is not None:
                    out[key] = round(r / 1024, 2)
        return out

    def reset(self):
        self.rates.reset()   # new pid -> new stream, counters start over


@register
class IOSBatteryCollector(Collector):
    name = "ios.battery"
    platforms = ("ios",)
    needs_process = False
    metrics = [MetricDef("battery_level", "电量", "%", "higher", 50, min=0, max=100),
               MetricDef("battery_temp", "电池温度", "°C", "lower", 51),
               MetricDef("current", "电流", "mA", "lower", 52)]
    MIN_INTERVAL = 5   # the battery gauge updates slowly; don't open a service every second

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.last = (0.0, {})

    def sample(self, tick):
        now = time.time()
        if now - self.last[0] < self.MIN_INTERVAL:
            return {}
        dev = self.ctx.device
        values = battery_values(dev._call(dev.agent.battery(), timeout=15))
        self.last = (now, values)
        return values


# ---------------------------------------------------------------- source

@register
class SyslogSource(Source):
    """Business metrics and markers from the app's os_log / NSLog output.

    Note: os_log hides dynamic strings as <private> unless logged with %{public}@,
    NSLog and print-to-NSLog are always visible."""
    name = "ios.syslog"
    label = "App 日志中的业务指标"
    platforms = ("ios",)

    def start(self):
        cfg = self.ctx.config
        self.rules = compile_rules(cfg.get("log_rules"))
        import re
        self.mark_rx = re.compile(cfg.get("mark_rule") or DEFAULT_MARK_RULE)
        self.stop_ev = threading.Event()
        self.process_name = self.ctx.device.executable(self.ctx.app)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop_ev.is_set():
            try:
                syslog_lines(self.ctx.device.agent, self.process_name, self._on_line, self.stop_ev)
            except Exception as e:
                self.emit_event(int(time.time() * 1000), "error", f"读取 iOS 日志中断: {e}")
                self.stop_ev.wait(3)

    def _on_line(self, line):
        r = match_line(line, self.rules, self.mark_rx)
        if not r:
            return
        ts = int(time.time() * 1000)
        if r[0] == "metric":
            self.emit_metric(ts, r[1], r[2])
        else:
            self.emit_event(ts, "marker", r[1])

    def stop(self):
        self.stop_ev.set()

"""Built-in Android platform. Implemented entirely through the public plugin API,
so it doubles as the reference for writing new collectors and sources."""
import re
import threading
import time

from ...logrules import compile_rules, match_line, page_key
from ...plugin import (Collector, Device, MetricDef, Platform, RateTracker, Source, Tick,
                       register)
from . import adb
from .parsers import (JankDetector, normalize_current_ma, parse_battery, parse_crash_blocks, parse_devices,
                      parse_displayed,
                      parse_framestats,
                      parse_gfxinfo, parse_meminfo_pss_mb, parse_net_dev, parse_proc_cpu, parse_total_cpu)

SEP = "___PERFSCOPE_SEP___"


class AndroidDevice(Device):
    platform = "android"

    def shell(self, command, timeout=15):
        return adb.shell(self.serial, command, timeout=timeout)

    def screenshot(self):
        return adb.run_bytes(["exec-out", "screencap", "-p"], serial=self.serial, timeout=10)

    def cold_start(self, app):
        # -S force-stops first (cold start); -W waits for the first frame and prints TotalTime
        comp = self.shell(f"cmd package resolve-activity --brief {app} | tail -n 1").strip()
        if "/" not in comp:
            raise RuntimeError(f"找不到 {app} 的启动页")
        out = self.shell(f"am start -S -W -n {comp}", timeout=60)
        m = re.search(r"TotalTime:\s*(\d+)", out)
        if not m:
            raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else "am start 没有返回启动耗时")
        return float(m.group(1))

    def crashes(self, app, state=None):
        # the crash buffer is small (Java FATAL EXCEPTION + native tombstone summaries), so diff full dumps
        lines = adb.run(["logcat", "-b", "crash", "-d", "-v", "threadtime"], serial=self.serial).splitlines()
        if state is None:
            return [], set(lines)
        return parse_crash_blocks([l for l in lines if l not in state], app), state | set(lines)

    def info(self):
        def prop(name):
            try:
                return self.shell(f"getprop {name}").strip()
            except adb.AdbError:
                return ""
        return {"platform": "android", "serial": self.serial,
                "brand": prop("ro.product.brand"), "model": prop("ro.product.model"),
                "os_version": prop("ro.build.version.release"),
                "sdk": prop("ro.build.version.sdk")}

    def list_apps(self, third_party=True):
        out = self.shell("pm list packages" + (" -3" if third_party else ""))
        return sorted(l.split(":", 1)[1].strip() for l in out.splitlines()
                      if l.startswith("package:"))

    def app_version(self, app):
        try:
            m = re.search(r"versionName=(\S+)", self.shell(f"dumpsys package {app}"))
            return m.group(1) if m else ""
        except adb.AdbError:
            return ""

    def pid(self, app):
        try:
            out = self.shell(f"pidof {app}").strip()
            if out:
                return int(out.split()[0])
        except (adb.AdbError, ValueError):
            pass
        try:  # old devices without pidof
            for line in self.shell("ps -A 2>/dev/null || ps").splitlines():
                cols = line.split()
                if cols and cols[-1] == app:
                    return int(cols[1])
        except (adb.AdbError, ValueError):
            pass
        return None

    def read_files(self, paths):
        if not paths:
            return {}
        # trailing "true": a missing file (e.g. current_now on some models) must not fail the batch
        cmd = f"; echo {SEP}; ".join(f"cat {p} 2>/dev/null" for p in paths) + "; true"
        chunks = self.shell(cmd).split(SEP)
        chunks += [""] * (len(paths) - len(chunks))
        return {p: c.strip("\n") for p, c in zip(paths, chunks)}


@register
class AndroidPlatform(Platform):
    name = "android"
    label = "Android"

    def list_devices(self):
        return parse_devices(adb.run(["devices", "-l"]))

    def device(self, serial):
        return AndroidDevice(serial)


# ---------------------------------------------------------------- built-in collectors

@register
class CpuCollector(Collector):
    name = "android.cpu"
    platforms = ("android",)
    metrics = [MetricDef("cpu", "App CPU", "%", "lower", 10)]

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.prev = None

    def files(self, pid):
        return ["/proc/stat", f"/proc/{pid}/stat"]

    def sample(self, tick):
        total_txt, proc_txt = tick.files.get("/proc/stat"), tick.files.get(f"/proc/{tick.pid}/stat")
        if not total_txt or not proc_txt:
            return {}
        total, proc = parse_total_cpu(total_txt), parse_proc_cpu(proc_txt)
        prev, self.prev = self.prev, (proc, total)
        if prev and total > prev[1]:
            return {"cpu": round(max(0, proc - prev[0]) / (total - prev[1]) * 100, 2)}
        return {}

    def reset(self):
        self.prev = None


@register
class MemoryCollector(Collector):
    name = "android.memory"
    platforms = ("android",)
    metrics = [MetricDef("mem_pss", "App 内存 (PSS)", "MB", "lower", 20)]

    def sample(self, tick):
        v = parse_meminfo_pss_mb(self.ctx.device.shell(f"dumpsys meminfo {tick.pid}"))
        return {"mem_pss": v} if v is not None else {}


@register
class GfxCollector(Collector):
    """FPS plus PerfDog-style Jank / BigJank / stutter from per-frame timings.
    framestats only keeps the last ~120 frames, so keep the sampling interval at 2 s or less."""
    name = "android.gfx"
    platforms = ("android",)
    metrics = [MetricDef("fps", "FPS", "帧/秒", "higher", 30),
               MetricDef("jank_count", "Jank", "次/分", "lower", 31),
               MetricDef("big_jank", "BigJank", "次/分", "lower", 32),
               MetricDef("stutter", "卡顿率", "%", "lower", 33)]

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.rates = RateTracker()
        self.jank = JankDetector()
        self.prev_ts = None

    def sample(self, tick):
        text = self.ctx.device.shell(f"dumpsys gfxinfo {self.ctx.app} framestats")
        frames, _ = parse_gfxinfo(text)
        out = {}
        if frames is not None:
            r = self.rates.rate("frames", frames, tick.ts)
            if r is not None:
                out["fps"] = round(r, 1)
        jank, big, jank_ns, _ = self.jank.feed(parse_framestats(text))
        prev, self.prev_ts = self.prev_ts, tick.ts
        if prev is not None and tick.ts > prev:
            per_min = 60_000 / (tick.ts - prev)
            out["jank_count"] = round(jank * per_min, 2)
            out["big_jank"] = round(big * per_min, 2)
            out["stutter"] = round(min(100.0, jank_ns / 1e6 / (tick.ts - prev) * 100), 2)
        return out

    def reset(self):
        self.rates.reset()
        self.jank.reset()
        self.prev_ts = None


@register
class NetCollector(Collector):
    name = "android.net"
    platforms = ("android",)
    needs_process = False
    metrics = [MetricDef("net_rx", "下行流量(整机)", "KB/s", "lower", 40),
               MetricDef("net_tx", "上行流量(整机)", "KB/s", "lower", 41)]

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.rates = RateTracker()

    def files(self, pid):
        return ["/proc/net/dev"]

    def sample(self, tick):
        txt = tick.files.get("/proc/net/dev")
        if not txt:
            return {}
        rx, tx = parse_net_dev(txt)
        out = {}
        for key, val in (("net_rx", rx), ("net_tx", tx)):
            r = self.rates.rate(key, val, tick.ts)
            if r is not None:
                out[key] = round(r / 1024, 2)
        return out


@register
class BatteryCollector(Collector):
    name = "android.battery"
    platforms = ("android",)
    needs_process = False
    metrics = [MetricDef("battery_level", "电量", "%", "higher", 50, min=0, max=100),
               MetricDef("battery_temp", "电池温度", "°C", "lower", 51)]

    def sample(self, tick):
        level, temp = parse_battery(self.ctx.device.shell("dumpsys battery"))
        out = {}
        if level is not None:
            out["battery_level"] = level
        if temp is not None:
            out["battery_temp"] = temp
        return out


@register
class CurrentCollector(Collector):
    name = "android.current"
    platforms = ("android",)
    needs_process = False
    metrics = [MetricDef("current", "电流", "mA", "lower", 52)]
    PATH = "/sys/class/power_supply/battery/current_now"

    def files(self, pid):
        return [self.ctx.plugin_config(self.name).get("path", self.PATH)]

    def sample(self, tick):
        raw = tick.files.get(self.ctx.plugin_config(self.name).get("path", self.PATH), "").strip()
        return {"current": normalize_current_ma(raw)} if raw.lstrip("-").isdigit() else {}


# ---------------------------------------------------------------- built-in source

@register
class LogcatSource(Source):
    """Business metrics and markers from logcat. Rules come from config log_rules."""
    name = "android.logcat"
    label = "App 日志中的业务指标"
    platforms = ("android",)

    def start(self):
        cfg = self.ctx.config
        self.rules = compile_rules(cfg.get("log_rules"))
        self.mark_rx = re.compile(cfg["mark_rule"]) if cfg.get("mark_rule") else None
        if self.mark_rx is None:
            from ...logrules import DEFAULT_MARK_RULE
            self.mark_rx = re.compile(DEFAULT_MARK_RULE)
        # -T 1: skip history, follow new lines only
        self.proc = adb.popen(["logcat", "-v", "brief", "-T", "1"], serial=self.ctx.device.serial)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        anr = f"ANR in {self.ctx.app}"
        for line in self.proc.stdout:
            page = parse_displayed(line, self.ctx.app)   # system-measured Activity load time
            if page:
                self.emit_metric(int(time.time() * 1000), page_key(page[0]), float(page[1]))
                continue
            if anr in line:
                self.emit_event(int(time.time() * 1000), "anr", "ANR（应用无响应）：" + line.split(anr, 1)[1].strip(" ()")[:200])
                continue
            r = match_line(line, self.rules, self.mark_rx)
            if not r:
                continue
            ts = int(time.time() * 1000)
            if r[0] == "metric":
                self.emit_metric(ts, r[1], r[2])
            else:
                self.emit_event(ts, "marker", r[1])

    def stop(self):
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

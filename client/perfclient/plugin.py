"""PerfScope extension API.

Everything a plugin needs is in this module. There are four kinds of plugin:

    Platform   how to find devices and talk to them (Android and iOS are built in)
    Collector  produces metric values on every sampling tick
    Source     pushes metrics/events asynchronously (e.g. logcat business logs)
    Hook       runs at session start / end (e.g. notify a chat group)

Register with the @register decorator. Plugins are loaded from:
    1. built-in platforms (perfclient/platforms/android, perfclient/platforms/ios)
    2. Python entry points in group "perfscope.plugins"
    3. *.py files in client/plugins/, ~/.perfscope/plugins/ and $PERFSCOPE_PLUGIN_DIRS
"""
import importlib
import importlib.util
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("perfscope.plugin")

BIZ_PREFIX = "biz."


# ---------------------------------------------------------------- metric definitions

@dataclass
class MetricDef:
    key: str
    label: str
    unit: str = ""
    better: str = "lower"  # "lower" or "higher": which direction is an improvement
    order: int = 500       # display order, smaller first
    min: Optional[float] = None   # fixed chart axis range, e.g. 0-100 for battery level
    max: Optional[float] = None

    def to_dict(self):
        d = {"label": self.label, "unit": self.unit, "better": self.better, "order": self.order}
        d.update({k: v for k, v in (("min", self.min), ("max", self.max)) if v is not None})
        return d


# ---------------------------------------------------------------- platform

class Device:
    """One connected device. Platforms subclass this."""
    platform = ""

    def __init__(self, serial: str):
        self.serial = serial

    def info(self) -> dict:
        """Environment recorded with every session: model, os_version, ..."""
        return {"platform": self.platform, "serial": self.serial}

    def list_apps(self) -> list:
        return []

    def app_version(self, app: str) -> str:
        return ""

    def pid(self, app: str) -> Optional[int]:
        return None

    def read_files(self, paths: list) -> dict:
        """Read several files in one round trip. Missing files map to ''."""
        return {}

    def shell(self, command: str, timeout: float = 15) -> str:
        raise NotImplementedError

    def screenshot(self) -> bytes:
        """Current screen as PNG or JPEG bytes. Optional: platforms without it get no screenshots."""
        raise NotImplementedError

    def cold_start(self, app: str) -> float:
        """Force-stop and launch the app; milliseconds until its first screen is drawn. Optional."""
        raise NotImplementedError

    def crashes(self, app: str, state=None):
        """Crash / kill reports of `app` that appeared since `state`.
        The first call (state=None) only takes a baseline and returns ([], state).
        -> ([{"id", "kind", "title", "text"}], new_state). Optional."""
        raise NotImplementedError


class Platform:
    name = ""
    label = ""

    def list_devices(self) -> list:
        """[{serial, model, state ('device' = ready), platform}]"""
        return []

    def device(self, serial: str) -> Device:
        raise NotImplementedError


# ---------------------------------------------------------------- session-time objects

class SessionContext:
    """What collectors / sources / hooks get to see about the running session."""

    def __init__(self, session_id, device: Device, app: str, config: dict):
        self.session_id = session_id
        self.device = device
        self.app = app
        self.config = config

    def plugin_config(self, name: str) -> dict:
        """Per-plugin settings from the session config:  plugins: {name: {...}}"""
        return (self.config.get("plugins") or {}).get(name) or {}


@dataclass
class Tick:
    ts: int                 # epoch ms of this sample
    pid: Optional[int]      # current process id of the app, None if not running
    files: dict             # contents of the files collectors asked for in files()


class RateTracker:
    """Helper for counters: turns cumulative values into per-second rates."""

    def __init__(self):
        self._prev = {}

    def rate(self, key, value, ts_ms):
        prev = self._prev.get(key)
        self._prev[key] = (value, ts_ms)
        if prev is None or ts_ms <= prev[1]:
            return None
        delta = value - prev[0]
        if delta < 0:  # counter reset
            delta = value
        return delta / ((ts_ms - prev[1]) / 1000)

    def delta(self, key, value):
        prev = self._prev.get(key)
        self._prev[key] = (value, 0)
        if prev is None:
            return None
        d = value - prev[0]
        return value if d < 0 else d

    def reset(self):
        self._prev.clear()


class Collector:
    """Produces values for the metrics it declares.

    Lifecycle per tick: files(pid) -> (platform reads them in one batch) -> sample(tick).
    Only metrics the user enabled are kept, so sample() may return extra keys.
    """
    name = ""
    platforms = ()          # e.g. ("android",)
    metrics: list = []      # list[MetricDef]
    needs_process = True    # skip this collector while the app is not running

    def __init__(self, ctx: SessionContext, enabled: set):
        self.ctx = ctx
        self.enabled = enabled

    def files(self, pid: Optional[int]) -> list:
        return []

    def sample(self, tick: Tick) -> dict:
        return {}

    def reset(self):
        """Called when the app process restarts; drop any per-process state."""


class Source:
    """Runs alongside sampling and pushes data whenever it has some."""
    name = ""
    label = ""
    platforms = ()
    default_enabled = True

    def __init__(self, ctx: SessionContext,
                 emit_metric: Callable[[int, str, float], None],
                 emit_event: Callable[[int, str, str], None]):
        self.ctx = ctx
        self.emit_metric = emit_metric   # (ts_ms, metric_key, value)
        self.emit_event = emit_event     # (ts_ms, type, label); type "marker" draws a line on charts

    def start(self):
        pass

    def stop(self):
        pass


class Hook:
    """Session lifecycle callbacks. Exceptions are caught and recorded as session events."""
    name = ""

    def on_session_start(self, session: dict):
        pass

    def on_session_end(self, session: dict, store):
        pass


# ---------------------------------------------------------------- registry

_REG = {"platform": {}, "collector": {}, "source": {}, "hook": {}}
LOADED = []        # [{"name", "origin"}]
LOAD_ERRORS = []   # [{"origin", "error"}]
_current_origin = "builtin"


def register(cls):
    """Class decorator. Later registrations with the same name replace earlier ones,
    so a plugin can override a built-in collector."""
    for kind, base in (("platform", Platform), ("collector", Collector),
                       ("source", Source), ("hook", Hook)):
        if isinstance(cls, type) and issubclass(cls, base):
            if not cls.name:
                raise ValueError(f"{cls.__name__} 需要设置 name")
            if cls.name in _REG[kind]:
                log.info("插件 %s 覆盖了已有的 %s", cls.name, kind)
            _REG[kind][cls.name] = cls
            cls._origin = _current_origin
            return cls
    raise TypeError(f"{cls} 不是 Platform / Collector / Source / Hook 的子类")


def get_platform(name: str) -> Platform:
    cls = _REG["platform"].get(name)
    if not cls:
        raise ValueError(f"不支持的平台: {name}")
    return cls()


def platforms() -> list:
    return [cls() for cls in _REG["platform"].values()]


def collectors(platform: str) -> list:
    return [c for c in _REG["collector"].values() if platform in c.platforms]


def sources(platform: str) -> list:
    return [s for s in _REG["source"].values() if platform in s.platforms]


def hooks() -> list:
    return list(_REG["hook"].values())


def metric_owners(platform: str) -> dict:
    """metric key -> collector class. Last registered wins."""
    owners = {}
    for c in collectors(platform):
        for m in c.metrics:
            owners[m.key] = c
    return owners


def metric_defs(platform: str) -> dict:
    defs = {}
    for c in collectors(platform):
        for m in c.metrics:
            defs[m.key] = m
    return dict(sorted(defs.items(), key=lambda kv: (kv[1].order, kv[0])))


def metric_meta(key: str, platform: str = "android", overrides: Optional[dict] = None) -> dict:
    """Display info for any metric, including business metrics from logs."""
    ov = (overrides or {}).get(key) or {}
    d = metric_defs(platform).get(key)
    if d:
        base = d.to_dict()
    elif key.startswith("page."):
        base = {"label": f"页面加载：{key[5:]}", "unit": "ms", "better": "lower", "order": 900}
    else:
        base = {"label": key[len(BIZ_PREFIX):] if key.startswith(BIZ_PREFIX) else key,
                "unit": "", "better": "lower", "order": 1000}
    return {**base, **{k: v for k, v in ov.items() if k in ("label", "unit", "better", "order", "min", "max")}}


# ---------------------------------------------------------------- loading

def _plugin_dirs():
    from .storage import data_dir
    dirs = [Path(__file__).resolve().parents[1] / "plugins", data_dir() / "plugins"]
    for d in os.environ.get("PERFSCOPE_PLUGIN_DIRS", "").split(os.pathsep):
        if d.strip():
            dirs.append(Path(d.strip()))
    return dirs


def _load_file(path: Path):
    global _current_origin
    _current_origin = str(path)
    try:
        spec = importlib.util.spec_from_file_location(f"perfscope_plugin_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        LOADED.append({"name": path.stem, "origin": str(path)})
    except Exception as e:  # a broken plugin must not break the tool
        LOAD_ERRORS.append({"origin": str(path), "error": f"{e}\n{traceback.format_exc(limit=3)}"})
    finally:
        _current_origin = "builtin"


_loaded = False


def load_plugins():
    global _loaded, _current_origin
    if _loaded:
        return
    _loaded = True
    for builtin in ("android", "ios"):  # built-in platforms
        _current_origin = "builtin"
        try:
            importlib.import_module(f"perfclient.platforms.{builtin}")
        except Exception as e:
            LOAD_ERRORS.append({"origin": f"builtin:{builtin}", "error": str(e)})
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="perfscope.plugins"):
            _current_origin = f"entry_point:{ep.name}"
            try:
                ep.load()
                LOADED.append({"name": ep.name, "origin": _current_origin})
            except Exception as e:
                LOAD_ERRORS.append({"origin": _current_origin, "error": str(e)})
        _current_origin = "builtin"
    except Exception:
        pass
    for d in _plugin_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                if not f.name.startswith("_"):
                    _load_file(f)


def describe() -> dict:
    """For the UI / API: what is installed."""
    def info(kind):
        return [{"name": c.name, "origin": getattr(c, "_origin", "builtin"),
                 "platforms": list(getattr(c, "platforms", ())),
                 "metrics": [m.key for m in getattr(c, "metrics", [])]}
                for c in _REG[kind].values()]
    return {"platforms": info("platform"), "collectors": info("collector"),
            "sources": info("source"), "hooks": info("hook"),
            "loaded_files": LOADED, "errors": LOAD_ERRORS}

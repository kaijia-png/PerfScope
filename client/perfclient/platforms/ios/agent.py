"""Long-lived connection to one iOS device, built on pymobiledevice3.

pymobiledevice3 is asyncio-only, while PerfScope's sampling loop is threaded. Each device gets
one IOSAgent that owns an event loop in a daemon thread; the platform and collectors call into
it synchronously with call(). Instruments streams (sysmon, fps, network) run as background tasks
that keep the latest sample; collectors read it on every tick. A stream nobody has read for
IDLE_S seconds is stopped, so streams end shortly after the session does.
"""
import asyncio
import concurrent.futures
import dataclasses
import threading
import time

IDLE_S = 15
FRESH_S = 5   # samples older than this are treated as missing


class IOSError(RuntimeError):
    pass


def _lib():
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        raise IOSError("没有安装 pymobiledevice3，请重新运行启动脚本安装依赖")


def friendly_error(e: Exception) -> str:
    """Turn pymobiledevice3 exceptions into something a tester can act on."""
    name = type(e).__name__
    hints = {
        "PairingDialogResponsePendingError": "请在 iPhone 上点「信任此电脑」并输入锁屏密码",
        "NotPairedError": "iPhone 还没有信任这台电脑，请解锁手机并点「信任」",
        "PasswordRequiredError": "请先解锁 iPhone",
        "UserDeniedPairingError": "iPhone 上拒绝了信任，请拔插数据线后重新点「信任」",
        "DeveloperModeIsNotEnabledError": "请在 iPhone「设置 → 隐私与安全性 → 开发者模式」中打开开发者模式，重启后再试",
        "DeveloperModeError": "请在 iPhone「设置 → 隐私与安全性 → 开发者模式」中打开开发者模式，重启后再试",
        "DeviceNotFoundError": "找不到设备，请检查数据线连接",
        "ConnectionFailedToUsbmuxdError": "无法连接 usbmuxd：请确认 iPhone 已用数据线连接（Windows 需安装 Apple 设备支持/iTunes）",
    }
    return hints.get(name) or (f"{name}: {e}" if str(e) else name)


class _Stream:
    def __init__(self):
        self.task = None
        self.last_read = 0.0
        self.value = None      # latest sample
        self.ts = 0.0          # when value was produced
        self.error = None


class IOSAgent:
    _agents = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, udid: str) -> "IOSAgent":
        with cls._lock:
            a = cls._agents.get(udid)
            if a is None:
                a = cls._agents[udid] = IOSAgent(udid)
            return a

    def __init__(self, udid):
        _lib()
        self.udid = udid
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True,
                         name=f"ios-{udid[:8]}").start()
        self.lockdown = None
        self.provider = None     # lockdown (< iOS 17) or RSD over a userspace tunnel (17+)
        self.tunnel = None
        self.dvt = None
        self.pctl = None
        self.shot = None
        self.ncpu = None
        self.streams = {}        # name -> _Stream
        self._conn_lock = None   # created inside the loop
        self.call(self._start_watchdog(), timeout=5)

    # ------------------------------------------------------------ sync bridge
    def call(self, coro, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise IOSError("iOS 设备响应超时")

    # ------------------------------------------------------------ connection
    async def _start_watchdog(self):
        self._conn_lock = asyncio.Lock()
        self.loop.create_task(self._watchdog())

    async def ensure_lockdown(self):
        if self.lockdown is None:
            from pymobiledevice3.lockdown import create_using_usbmux
            self.lockdown = await create_using_usbmux(serial=self.udid, autopair=True)
        return self.lockdown

    async def ensure_dvt(self):
        async with self._conn_lock:
            if self.dvt is not None:
                return self.dvt
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.mobile_image_mounter import auto_mount
            ld = await self.ensure_lockdown()
            major = int(ld.product_version.split(".")[0])
            if major >= 17:
                self.provider = await self._open_tunnel()
            else:
                self.provider = ld
            try:
                await auto_mount(self.provider)
            except Exception:
                # "already mounted" is raised as an error by some versions; a real mount
                # failure surfaces again when DVT fails to connect below.
                pass
            dvt = DvtProvider(self.provider)
            await dvt.__aenter__()
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            pctl = ProcessControl(dvt)
            await pctl.__aenter__()
            self.dvt, self.pctl = dvt, pctl
            return dvt

    async def _open_tunnel(self):
        """iOS 17+: developer services are reachable only through an RSD tunnel. The userspace
        tunnel needs no root; iOS 17.0-17.3 can't use it, macOS then falls back to remoted's."""
        from pymobiledevice3.exceptions import UserspaceTunnelUnavailableError
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        try:
            self.tunnel = UserspaceRsdTunnel(serial=self.udid, remotepairing_fallback=False)
            return await self.tunnel.aopen()
        except UserspaceTunnelUnavailableError:
            self.tunnel = None
            import platform
            if platform.system() != "Darwin":
                raise IOSError("iOS 17.0–17.3 需要先以管理员身份运行：pymobiledevice3 remote tunneld")
            from pymobiledevice3.remote.native_tunnel import establish_native_rsd
            return await establish_native_rsd(serial=self.udid)

    async def reset(self):
        """Drop every connection; the next call reconnects (cable re-plugged, device rebooted)."""
        for s in self.streams.values():
            if s.task:
                s.task.cancel()
        self.streams.clear()
        for obj in (self.shot, self.pctl, self.dvt):
            try:
                if obj is not None:
                    await obj.__aexit__(None, None, None)
            except Exception:
                pass
        for closer in (getattr(self.tunnel, "aclose", None), getattr(self.lockdown, "close", None)):
            try:
                if closer:
                    await closer()
            except Exception:
                pass
        self.lockdown = self.provider = self.tunnel = self.dvt = self.pctl = self.shot = None

    # ------------------------------------------------------------ one-shot queries
    async def info(self):
        ld = await self.ensure_lockdown()
        v = ld.all_values
        return {"platform": "ios", "serial": self.udid, "brand": "Apple",
                "model": v.get("ProductType", ""), "name": v.get("DeviceName", ""),
                "os_version": v.get("ProductVersion", ""), "build": v.get("BuildVersion", "")}

    async def apps(self):
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        ld = await self.ensure_lockdown()
        async with InstallationProxyService(ld) as svc:
            return await svc.get_apps(application_type="User")

    async def pid(self, bundle_id):
        await self.ensure_dvt()
        p = await self.pctl.process_identifier_for_bundle_identifier(bundle_id)
        return int(p) if p and int(p) > 0 else None

    async def screenshot(self):
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
        await self.ensure_dvt()
        if self.shot is None:
            shot = Screenshot(self.dvt)
            await shot.__aenter__()
            self.shot = shot
        return await self.shot.get_screenshot()

    async def crash_reports(self, known):
        """New crash-report file names since `known` (None: just list), and the full current listing."""
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        ld = await self.ensure_lockdown()
        async with CrashReportsManager(ld) as cm:
            await cm.flush()   # move pending reports into the CrashReports folder first
            names = set(await cm.ls("/", depth=1))
            texts = {}
            if known is not None:
                for n in sorted(names - known):
                    if n.endswith((".ips", ".crash")):
                        texts[n] = (await cm.afc.get_file_contents(n)).decode(errors="replace")
        return names, texts

    async def battery(self):
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        ld = await self.ensure_lockdown()
        async with DiagnosticsService(ld) as svc:
            return await svc.get_battery() or {}

    # ------------------------------------------------------------ streams
    def latest(self, name, factory, *args):
        """Latest value of a stream, starting it if needed. None if nothing fresh yet."""
        return self.call(self._latest(name, factory, args), timeout=60)

    async def _latest(self, name, factory, args):
        s = self.streams.get(name)
        if s is None or s.task is None or s.task.done():
            if s is not None and s.error:
                err, s.error = s.error, None
                self.streams.pop(name, None)
                raise IOSError(err)
            s = self.streams[name] = _Stream()
            s.task = self.loop.create_task(self._run_stream(s, factory, args))
        s.last_read = time.time()
        return s.value if time.time() - s.ts <= FRESH_S else None

    async def _run_stream(self, s, factory, args):
        try:
            await factory(self, s, *args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            s.error = friendly_error(e)

    def stop_stream(self, name):
        async def _stop():
            s = self.streams.pop(name, None)
            if s and s.task:
                s.task.cancel()
        self.call(_stop(), timeout=5)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for name, s in list(self.streams.items()):
                if s.task and not s.task.done() and now - s.last_read > IDLE_S:
                    s.task.cancel()
                    self.streams.pop(name, None)


# ---------------------------------------------------------------- stream bodies

async def sysmon_stream(agent: IOSAgent, s: _Stream):
    """Per-process CPU / memory / threads for every process, about once a second."""
    from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
    dvt = await agent.ensure_dvt()
    sysmon = await Sysmontap.create(dvt, interval=1000)
    async with sysmon:
        async for row in sysmon:
            if not isinstance(row, dict):
                continue
            agent.ncpu = row.get("CPUCount") or row.get("EnabledCPUs") or agent.ncpu
            procs = row.get("Processes")
            if not procs:
                continue
            by_pid = {}
            for pid, values in procs.items():
                try:
                    by_pid[int(pid)] = dataclasses.asdict(sysmon.process_attributes_cls(*values))
                except Exception:
                    continue
            s.value, s.ts = by_pid, time.time()


async def fps_stream(agent: IOSAgent, s: _Stream):
    """Whole-display Core Animation FPS (iOS exposes no per-app frame counter without Xcode)."""
    from pymobiledevice3.services.dvt.instruments.graphics import Graphics
    dvt = await agent.ensure_dvt()
    async with Graphics(dvt) as g:
        async for fps in g.fps():
            s.value, s.ts = fps, time.time()


async def net_stream(agent: IOSAgent, s: _Stream, pid: int):
    """Cumulative per-process network counters for one pid."""
    from pymobiledevice3.services.dvt.instruments.network_statistics import NetworkStatistics
    dvt = await agent.ensure_dvt()
    async with NetworkStatistics(dvt, [pid]) as ns:
        async for sample in ns:
            rec = sample.get(pid) or sample.get(str(pid))
            if rec:
                s.value, s.ts = rec, time.time()


def syslog_lines(agent: IOSAgent, process_name: str, on_line, stop: threading.Event):
    """Run in a plain thread: forward log messages of one process until stop is set."""
    async def _run():
        from pymobiledevice3.services.os_trace import OsTraceService
        ld = await agent.ensure_lockdown()
        svc = OsTraceService(ld)
        async for entry in svc.syslog(pid=-1):
            if stop.is_set():
                break
            fname = (entry.filename or "").rsplit("/", 1)[-1]
            if fname == process_name:
                on_line(entry.message)
    fut = asyncio.run_coroutine_threadsafe(_run(), agent.loop)
    while not stop.is_set() and not fut.done():
        stop.wait(0.5)
    fut.cancel()
    if fut.done() and not fut.cancelled() and fut.exception():
        raise IOSError(friendly_error(fut.exception()))

#!/usr/bin/env python3
"""Simulated adb for demos and development without a phone.

    export ADB_PATH=/path/to/tools/fake_adb.py   (chmod +x first)

Simulates one device running com.example.recorder: slowly leaking memory,
~55 fps while active, battery draining, and PERF| business logs on logcat.
"""
import math
import os
import random
import sys
import tempfile
import time

SERIAL = "FAKE0001"
PKG = "com.example.recorder"
base_file = os.path.join(tempfile.gettempdir(), "fake_adb_base")
if not os.path.exists(base_file):
    open(base_file, "w").write(str(time.time()))
BASE = float(open(base_file).read())
t = time.time()
el = t - BASE


def read_file(path):
    if path == "/proc/stat":
        return f"cpu  {int(t * 800)} 0 0 0 0 0 0 0 0 0\ncpu0 1 0 0 0 0 0 0 0"
    if path == "/proc/12345/stat":
        proc = int(t * 120 + 40 * math.sin(t / 9))
        return f"12345 ({PKG}) S 1 1 0 0 -1 0 0 0 0 0 {proc} 0 0 0"
    if path == "/proc/12345/status":
        return f"Name:\t{PKG[:15]}\nThreads:\t{60 + int(el / 20)}\nVmRSS:\t200000 kB"
    if path == "/proc/net/dev":
        rx, tx = int(t * 18000 + 5000 * math.sin(t / 5)), int(t * 42000)
        return ("Inter-|   Receive\n face |bytes packets\n    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
                f" wlan0: {rx} 10 0 0 0 0 0 0 {tx} 10 0 0 0 0 0 0")
    if path.endswith("current_now"):
        return str(int(-(320000 + 40000 * math.sin(t / 11))))
    return ""


def shell(cmd):
    if cmd.startswith("getprop"):
        return {"ro.product.brand": "Xiaomi", "ro.product.model": "Simulated 14",
                "ro.build.version.release": "14", "ro.build.version.sdk": "34"}.get(cmd.split()[1], "")
    if cmd.startswith("pm list packages"):
        return f"package:{PKG}\npackage:com.example.agent\n"
    if cmd.startswith("dumpsys package"):
        return "    versionName=2.3.0-demo\n"
    if cmd.startswith("pidof"):
        return "12345\n" if PKG in cmd else ""
    if "___PERFSCOPE_SEP___" in cmd:
        parts = cmd.split("; echo ___PERFSCOPE_SEP___; ")
        return "\n___PERFSCOPE_SEP___\n".join(read_file(p.split()[1]) for p in parts)
    if cmd.startswith("dumpsys meminfo"):
        pss = int((180 + el * 0.05 + 6 * math.sin(t / 13)) * 1024)
        return f"** MEMINFO in pid 12345 [{PKG}] **\n           TOTAL PSS:   {pss}   TOTAL RSS: {pss + 9000}\n"
    if cmd.startswith("dumpsys gfxinfo"):
        out = f"Total frames rendered: {int(t * 55)}\nJanky frames: {int(t * 1.1)} (2.0%)\n"
        if "framestats" in cmd:
            out += framestats()
        return out
    if cmd.startswith("cmd package resolve-activity"):
        return f"{PKG}/.MainActivity\n"
    if cmd.startswith("am start"):
        return f"Starting: Intent {{ cmp={PKG}/.MainActivity }}\nStatus: ok\nLaunchState: COLD\nActivity: {PKG}/.MainActivity\nTotalTime: {int(random.gauss(620, 40))}\nWaitTime: 640\nComplete\n"
    if cmd.startswith("dumpsys battery"):
        return (f"  level: {max(5, int(90 - el / 120))}\n"
                f"  temperature: {int(330 + 40 * min(1, el / 900))}\n")
    return ""


def framestats():
    """Last 120 frames at 60 Hz; frame times ~12 ms, a 100 ms hitch every 7 s and a 150 ms one every 20 s."""
    period = 1_000_000_000 // 60
    newest = int(t * 1e9) // period
    rows = ["---PROFILEDATA---", "Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,"
            "AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,"
            "SwapBuffers,FrameCompleted,"]
    for k in range(newest - 119, newest + 1):
        iv = k * period
        sec = iv / 1e9
        ft = 12_000_000 + (k % 7) * 400_000
        if int(sec) % 20 == 0 and k % 60 == 0:
            ft = 150_000_000
        elif int(sec) % 7 == 0 and k % 60 == 0:
            ft = 100_000_000
        rows.append(f"0,{iv},{iv},0,0,{iv},{iv},{iv},{iv},{iv},{iv},{iv},{iv + ft - 1000},{iv + ft},")
    rows.append("---PROFILEDATA---")
    return "\n".join(rows) + "\n"


def logcat():
    sys.stdout.reconfigure(line_buffering=True)
    print("I/Recorder( 12345): PERF_MARK|开始录音")
    n = 0
    while True:
        time.sleep(random.uniform(0.8, 2.0))
        n += 1
        print(f"I/Recorder( 12345): PERF|asr_latency|{int(random.gauss(380, 60))}")
        if n % 5 == 0:
            print(f"I/Agent( 12345): PERF|agent_ttft|{int(random.gauss(1400, 350))}")
        if n % 30 == 0:
            print("I/Agent( 12345): PERF_MARK|发起 Agent 提问")
        if n % 4 == 0:   # app-reported page loads, and the system's own Activity timing
            page, base = random.choice([("录音列表", 260), ("录音详情", 520), ("Agent 对话", 780)])
            print(f"I/Recorder( 12345): PERF_PAGE|{page}|{int(random.gauss(base, base * 0.15))}")
        if n % 9 == 0:
            ms = int(random.gauss(410, 60))
            print(f"I/ActivityTaskManager( 1000): Displayed {PKG}/.SettingsActivity for user 0: +{ms}ms")


args = sys.argv[1:]
if args[:1] == ["-s"]:
    args = args[2:]
if args[:1] == ["devices"]:
    print(f"List of devices attached\n{SERIAL}  device product:sim model:Simulated_14 device:sim")
elif args[:1] == ["shell"]:
    sys.stdout.write(shell(" ".join(args[1:])))
elif args[:3] == ["exec-out", "screencap", "-p"]:
    # a phone-shaped PNG whose colour and clock change every second, so frames are tellable apart
    import io
    import time
    from PIL import Image, ImageDraw
    t = time.time()
    im = Image.new("RGB", (540, 1170), (int(t * 40) % 200 + 30, 90, 160))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 900, 500, 1000), fill=(255, 255, 255))
    d.text((60, 930), time.strftime("%H:%M:%S", time.localtime(t)), fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    sys.stdout.buffer.write(buf.getvalue())
elif args[:1] == ["logcat"] and "-d" in args:
    pass   # crash buffer dump: the simulated app never crashes
elif args[:1] == ["logcat"]:
    try:
        logcat()
    except (KeyboardInterrupt, BrokenPipeError):
        pass

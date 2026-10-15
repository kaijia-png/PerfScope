"""Pure parsing functions for Android command output (unit tested)."""
import re


def parse_devices(out):
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        model = next((p.split(":", 1)[1].replace("_", " ") for p in parts[2:]
                      if p.startswith("model:")), "")
        devices.append({"serial": parts[0], "state": parts[1] if len(parts) > 1 else "unknown",
                        "model": model, "platform": "android"})
    return devices


def parse_total_cpu(text):
    line = text.splitlines()[0]
    return sum(int(x) for x in line.split()[1:9])


def parse_proc_cpu(stat_text):
    rest = stat_text.rsplit(")", 1)[1].split()  # comm may contain spaces
    return int(rest[11]) + int(rest[12])         # utime + stime


def parse_net_dev(text):
    rx = tx = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        cols = data.split()
        if iface.strip() == "lo" or len(cols) < 9:
            continue
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def parse_meminfo_pss_mb(text):
    m = re.search(r"TOTAL PSS:\s+(\d+)", text) or re.search(r"^\s*TOTAL\s+(\d+)", text, re.M)
    return round(int(m.group(1)) / 1024, 1) if m else None


def parse_gfxinfo(text):
    t = re.search(r"Total frames rendered:\s*(\d+)", text)
    j = re.search(r"Janky frames:\s*(\d+)", text)
    return (int(t.group(1)) if t else None, int(j.group(1)) if j else None)


def parse_battery(text):
    level = re.search(r"^\s*level:\s*(\d+)", text, re.M)
    temp = re.search(r"^\s*temperature:\s*(\d+)", text, re.M)
    return (int(level.group(1)) if level else None,
            int(temp.group(1)) / 10 if temp else None)


def normalize_current_ma(raw):
    v = abs(int(raw))
    return round(v / 1000, 1) if v > 10000 else float(v)  # most devices report µA


def parse_status_field(text, field):
    """/proc/<pid>/status  ->  value of e.g. 'Threads' or 'VmRSS' (int)"""
    m = re.search(rf"^{field}:\s+(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def parse_framestats(text):
    """`dumpsys gfxinfo <pkg> framestats` -> [(intended_vsync_ns, frame_time_ns)] of normally rendered frames.
    Each window has a ---PROFILEDATA--- block: a 'Flags,...' header, then one CSV row per frame."""
    frames, cols = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Flags,"):
            cols = [c for c in line.split(",") if c]
            continue
        if cols is None or not line[:1].isdigit():
            continue
        try:
            row = dict(zip(cols, (int(v) for v in line.split(",")[:len(cols)])))
        except ValueError:
            continue
        iv, done = row.get("IntendedVsync"), row.get("FrameCompleted")
        if row.get("Flags", 1) == 0 and iv is not None and done is not None and done > iv:   # non-zero flags: skipped / first frames
            frames.append((iv, done - iv))
    return frames


class JankDetector:
    """PerfDog-style jank on a stream of frame times.
    Jank: frame > 2x the average of the previous 3 frames and > 2 movie frames (83.3 ms).
    BigJank: same, but > 3 movie frames (125 ms); every BigJank is also a Jank."""
    MOVIE_NS = 1_000_000_000 / 24

    def __init__(self):
        self.last_vsync = -1   # vsync timestamps are >= 0
        self.recent = []

    def feed(self, frames):
        """-> (jank, big_jank, jank_time_ns, new_frames) for frames not seen before"""
        jank = big = jank_ns = n = 0
        for iv, ft in sorted(frames):
            if iv <= self.last_vsync:
                continue   # framestats repeats the last ~120 frames on every call
            n += 1
            if len(self.recent) == 3 and ft > 2 * sum(self.recent) / 3:
                if ft > 2 * self.MOVIE_NS:
                    jank += 1
                    jank_ns += ft
                    if ft > 3 * self.MOVIE_NS:
                        big += 1
            self.recent = (self.recent + [ft])[-3:]
            self.last_vsync = iv
        return jank, big, jank_ns, n

    def reset(self):
        self.__init__()


_THREADTIME = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(\d+)\s+\d+\s+\w\s+([^:]*?)\s*: ?(.*)$")
_EXC = re.compile(r"^((?:[\w$]+\.)+[\w$]*(?:Exception|Error|Throwable)\b.*)$")


def parse_crash_blocks(lines, app):
    """New lines of `logcat -b crash -v threadtime` -> crashes of `app`.
    Lines are grouped by the logging pid: the app itself for Java crashes, crash_dump for native ones."""
    blocks, cur, cur_pid = [], [], None
    for line in lines:
        m = _THREADTIME.match(line)
        pid = m.group(1) if m else cur_pid
        if cur and pid != cur_pid:
            blocks.append(cur)
            cur = []
        cur.append(line)
        cur_pid = pid
    if cur:
        blocks.append(cur)
    crashes = []
    for block in blocks:
        text = "\n".join(block)
        msgs = [(_THREADTIME.match(l).group(3) if _THREADTIME.match(l) else l).strip() for l in block]
        if f"Process: {app}," in text or f"Process: {app} " in text:          # Java
            exc = next((x for x in msgs if _EXC.match(x)), None)
            title = f"闪退：{exc or 'FATAL EXCEPTION'}"
        elif f">>> {app} <<<" in text or f"name: {app}" in text or f"Cmdline: {app}" in text:   # native
            sig = next((x for x in msgs if x.startswith("signal ")), "")
            abort = next((x for x in msgs if x.startswith("Abort message:")), "")
            title = "Native 闪退：" + (" · ".join(x for x in (sig, abort) if x) or "tombstone")
        else:
            continue
        crashes.append({"id": f"android:{hash(block[0])}", "kind": "crash", "title": title[:300], "text": text})
    return crashes


_DISPLAYED = re.compile(r"\b(?P<kind>Displayed|Fully drawn) (?P<pkg>[\w.]+)/(?P<act>[\w.$]+)"
                        r"(?: for user \d+)?: \+(?:(?P<s>\d+)s)?(?P<ms>\d+)ms")


def parse_displayed(line, app):
    """ActivityTaskManager 'Displayed pkg/.Activity: +1s234ms' (time to first frame of a new Activity)
    and 'Fully drawn …' (when the app calls reportFullyDrawn) -> (page name, ms) for `app`."""
    m = _DISPLAYED.search(line)
    if not m or m.group("pkg") != app:
        return None
    name = m.group("act").rsplit(".", 1)[-1]
    if m.group("kind") == "Fully drawn":
        name += "（完全绘制）"
    return name, int(m.group("s") or 0) * 1000 + int(m.group("ms"))

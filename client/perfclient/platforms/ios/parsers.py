"""Pure functions turning pymobiledevice3 samples into metric values (unit-tested without a device)."""


def proc_values(proc, ncpu=None):
    """One sysmontap process entry -> {cpu, mem_footprint, threads}.

    cpuUsage is percent of one core (Xcode shows up to 600% on a 6-core phone); divide by the
    core count so it matches Android's "percent of the whole device"."""
    out = {}
    cpu = proc.get("cpuUsage")
    if isinstance(cpu, (int, float)):
        out["cpu"] = round(cpu / ncpu if ncpu else cpu, 2)
    mem = proc.get("physFootprint")
    if not isinstance(mem, (int, float)):
        mem = proc.get("memResidentSize")
    if isinstance(mem, (int, float)):
        out["mem_footprint"] = round(mem / 1024 / 1024, 1)
    th = proc.get("threadCount")
    if isinstance(th, int):
        out["threads"] = th
    return out


def net_counters(rec):
    """NetworkStatistics record -> (rx_bytes, tx_bytes) cumulative, None if missing."""
    rx, tx = rec.get("net.rx.bytes"), rec.get("net.tx.bytes")
    return (rx if isinstance(rx, (int, float)) else None,
            tx if isinstance(tx, (int, float)) else None)


def battery_values(io):
    """IORegistry IOPMPowerSource -> {battery_level, battery_temp, current}."""
    out = {}
    cap, mx = io.get("CurrentCapacity"), io.get("MaxCapacity")
    if isinstance(cap, (int, float)):
        # newer iOS reports percent directly (MaxCapacity == 100); older reports mAh
        out["battery_level"] = round(cap / mx * 100) if mx and mx != 100 else int(cap)
    temp = io.get("Temperature")
    if isinstance(temp, (int, float)):
        out["battery_temp"] = round(temp / 100, 1)
    amp = io.get("InstantAmperage", io.get("Amperage"))
    if isinstance(amp, (int, float)):
        if amp > 2 ** 63:  # unsigned wrap of a negative value
            amp -= 2 ** 64
        out["current"] = float(abs(amp))
    return out


def _ips(text):
    """.ips = one JSON header line + a JSON body (iOS 15+). Older plain-text reports give an empty body."""
    import json
    first, _, rest = text.partition("\n")
    try:
        header = json.loads(first)
    except ValueError:
        header = {}
    try:
        body = json.loads(rest) if rest.lstrip().startswith("{") else {}
    except ValueError:
        body = {}
    return header, body


def is_app_report(filename, process):
    """Crash files are named <process>-<date>.ips, or <Kind>_<process>-<date>.ips (ExcUserFault_…)."""
    base = filename.rsplit("/", 1)[-1]
    return base.startswith(f"{process}-") or f"_{process}-" in base


def summarize_report(filename, text, process):
    """-> {"kind", "title"} for a report about `process`, or None when it is about something else."""
    base = filename.rsplit("/", 1)[-1]
    header, body = _ips(text)
    if base.startswith("JetsamEvent") or str(header.get("bug_type")) == "298":
        page = body.get("pageSize") or 16384
        for p in body.get("processes") or []:
            if p.get("name") == process and p.get("reason"):
                mb = round((p.get("rpages") or 0) * page / 1024 / 1024)
                return {"kind": "jetsam", "title": f"内存超限被系统终止（{p['reason']}）" + (f"，当时占用约 {mb} MB" if mb else "")}
        return None
    if not is_app_report(filename, process):
        return None
    exc = body.get("exception") or {}
    parts = [x for x in (exc.get("type"), exc.get("signal") and f"({exc['signal']})", exc.get("subtype")) if x]
    term_d = body.get("termination") or {}
    term = " ".join(x for x in (term_d.get("namespace"), term_d.get("indicator")) if x)
    frames = []
    threads = body.get("threads") or []
    ft = body.get("faultingThread")
    images = body.get("usedImages") or []
    if isinstance(ft, int) and ft < len(threads):
        for f in (threads[ft].get("frames") or [])[:3]:
            img = images[f["imageIndex"]].get("name") or "" if isinstance(f.get("imageIndex"), int) and f["imageIndex"] < len(images) else ""
            if f.get("symbol") or img:   # unsymbolicated system frames are just addresses; leave them to the full log
                frames.append(f"{img} {f.get('symbol') or ''}".strip())
    detail = " ".join(parts) or header.get("name") or ""
    if term and term not in detail:
        detail = f"{detail} · {term}" if detail else term
    if base.startswith("ExcUserFault") or base.startswith("ExcResource"):
        kind, label = "fault", "异常报告（未闪退）"
    else:
        kind, label = "crash", "闪退"
    title = f"{label}：{detail}" if detail else label
    if frames:
        title += " · " + frames[0]
    return {"kind": kind, "title": title[:300]}

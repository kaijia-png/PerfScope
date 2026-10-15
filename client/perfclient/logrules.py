"""Platform-independent log line matching for business metrics and markers.

A metric rule needs a `value` group and either a `name` group or a fixed "name".
A marker rule needs a `label` group.
"""
import re

BIZ_PREFIX = "biz."
# The app prints e.g.  PERF|asr_latency|320
DEFAULT_LOG_RULES = [{"pattern": r"PERF\|(?P<name>[\w.\-]+)\|(?P<value>-?\d+(?:\.\d+)?)"}]
# ... and markers:     PERF_MARK|开始录音
DEFAULT_MARK_RULE = r"PERF_MARK\|(?P<label>.+)$"
# ... and page loads:  PERF_PAGE|录音详情|420   (milliseconds from navigation to the page being usable)
PAGE_RULE = re.compile(r"PERF_PAGE\|(?P<page>[^|]+?)\|(?P<ms>\d+(?:\.\d+)?)")
PAGE_PREFIX = "page."


def page_key(name):
    """Page name -> metric key; one metric per page so stats, compare and trends work per page."""
    return PAGE_PREFIX + " ".join(name.split())[:60]   # keep the name as logged, just tidy whitespace


def compile_rules(rules):
    compiled = []
    for r in rules or DEFAULT_LOG_RULES:
        rx = re.compile(r["pattern"])
        if "value" not in rx.groupindex:
            raise ValueError(f"日志规则缺少 value 分组: {r['pattern']}")
        if "name" not in rx.groupindex and not r.get("name"):
            raise ValueError(f"日志规则需要 name 分组或 name 字段: {r['pattern']}")
        compiled.append((rx, r.get("name")))
    return compiled


def match_line(line, rules, mark_rx):
    """-> ('metric', key, value) / ('marker', label) / None"""
    m = PAGE_RULE.search(line)
    if m:
        return ("metric", page_key(m.group("page")), float(m.group("ms")))
    for rx, fixed in rules:
        m = rx.search(line)
        if m:
            try:
                return ("metric", BIZ_PREFIX + (fixed or m.group("name")), float(m.group("value")))
            except ValueError:
                return None
    if mark_rx is not None:
        m = mark_rx.search(line)
        if m:
            return ("marker", m.group("label").strip())
    return None

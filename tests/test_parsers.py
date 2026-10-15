"""python -m pytest tests  (从仓库根目录运行)"""
import re

from perfclient.platforms.android import parsers as android
from perfclient.logrules import DEFAULT_MARK_RULE, compile_rules, match_line
from perfclient.session import parse_alerts, parse_duration


def test_proc_cpu_with_spaces_in_name():
    stat = "123 (com.app:remote svc) S 1 1 0 0 -1 0 0 0 0 0 150 50 0 0"
    assert android.parse_proc_cpu(stat) == 200


def test_total_cpu():
    assert android.parse_total_cpu("cpu  10 20 30 40 50 60 70 80 90 100") == 360


def test_meminfo_new_and_old_format():
    assert android.parse_meminfo_pss_mb("   TOTAL PSS:   204800   TOTAL RSS: 1") == 200.0
    assert android.parse_meminfo_pss_mb("        TOTAL   102400    50000") == 100.0


def test_gfxinfo_and_battery():
    assert android.parse_gfxinfo("Total frames rendered: 900\nJanky frames: 12 (1.3%)") == (900, 12)
    assert android.parse_battery("  level: 77\n  temperature: 352\n") == (77, 35.2)


def test_net_dev_excludes_lo():
    txt = "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n wlan0: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0"
    assert android.parse_net_dev(txt) == (100, 200)


def test_status_field():
    assert android.parse_status_field("Name:\tx\nThreads:\t87\n", "Threads") == 87


def test_current_units():
    assert android.normalize_current_ma("-350000") == 350.0
    assert android.normalize_current_ma("420") == 420.0


def test_log_rules():
    rules = compile_rules(None)
    mark = re.compile(DEFAULT_MARK_RULE)
    assert match_line("I/X( 1): PERF|agent_ttft|1234.5", rules, mark) == ("metric", "biz.agent_ttft", 1234.5)
    assert match_line("I/X( 1): PERF_MARK|开始录音", rules, mark) == ("marker", "开始录音")
    assert match_line("random line", rules, mark) is None
    fixed = compile_rules([{"name": "upload_ms", "pattern": r"upload done in (?P<value>\d+)ms"}])
    assert match_line("upload done in 88ms", fixed, None) == ("metric", "biz.upload_ms", 88.0)


def test_alerts_and_duration():
    assert parse_alerts({"mem_pss": "> 800"})["mem_pss"][:2] == (">", 800.0)
    assert parse_duration("2h") == 7200 and parse_duration("30m") == 1800 and parse_duration(None) is None


def test_server_stats():
    from perfserver.stats import compute
    pts = [[i * 1000, "mem", 100 + i] for i in range(600)]
    s = compute(pts)["mem"]
    assert s["count"] == 600 and s["min"] == 100 and s["max"] == 699
    assert abs(s["slope_per_hour"] - 3600) < 1   # +1 MB/s = +3600 MB/h

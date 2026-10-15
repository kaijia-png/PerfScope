"""iOS 平台：解析函数和插件注册（不需要连接 iPhone）"""
from perfclient import plugin
from perfclient.platforms.ios import parsers as ios


def test_proc_values_normalizes_cpu_by_core_count():
    proc = {"pid": 1, "cpuUsage": 150.0, "physFootprint": 200 * 1024 * 1024, "threadCount": 42}
    assert ios.proc_values(proc, ncpu=6) == {"cpu": 25.0, "mem_footprint": 200.0, "threads": 42}
    assert ios.proc_values(proc)["cpu"] == 150.0  # core count unknown: raw value


def test_proc_values_skips_missing_fields():
    # the first sysmon snapshot has no cpuUsage yet
    assert ios.proc_values({"pid": 1, "cpuUsage": None, "memResidentSize": 1024 * 1024}) == {"mem_footprint": 1.0}


def test_net_counters():
    assert ios.net_counters({"net.rx.bytes": 1000, "net.tx.bytes": 20}) == (1000, 20)
    assert ios.net_counters({}) == (None, None)


def test_battery_values():
    io = {"CurrentCapacity": 87, "MaxCapacity": 100, "Temperature": 3125, "InstantAmperage": -412}
    assert ios.battery_values(io) == {"battery_level": 87, "battery_temp": 31.2, "current": 412.0}
    # older iOS: capacity in mAh, current as unsigned 64-bit
    io = {"CurrentCapacity": 1500, "MaxCapacity": 3000, "InstantAmperage": 2 ** 64 - 300}
    assert ios.battery_values(io) == {"battery_level": 50, "current": 300.0}


def test_ios_platform_registered():
    plugin.load_plugins()
    assert "ios" in {p.name for p in plugin.platforms()}
    defs = plugin.metric_defs("ios")
    assert {"cpu", "mem_footprint", "threads", "fps", "net_rx", "battery_temp"} <= set(defs)
    assert "mem_pss" not in defs and "jank" not in defs
    assert [s.name for s in plugin.sources("ios")] == ["ios.syslog"]
    # Android is unaffected
    assert "mem_pss" in plugin.metric_defs("android")

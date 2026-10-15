"""示例采集器插件：App 线程数。

线程数持续上涨通常意味着线程泄漏（Agent 多轮对话、反复开关录音时值得关注）。
这个文件放在 client/plugins/ 下就会被自动加载，不需要改任何核心代码。
"""
from perfclient.plugin import Collector, MetricDef, register
from perfclient.platforms.android.parsers import parse_status_field


@register
class ThreadsCollector(Collector):
    name = "android.threads"
    platforms = ("android",)
    metrics = [MetricDef("threads", "线程数", "个", "lower", order=25)]

    def files(self, pid):
        # 声明需要读的文件；所有采集器的文件会合并成一次 adb 调用读取
        return [f"/proc/{pid}/status"]

    def sample(self, tick):
        n = parse_status_field(tick.files.get(f"/proc/{tick.pid}/status", ""), "Threads")
        return {"threads": n} if n is not None else {}

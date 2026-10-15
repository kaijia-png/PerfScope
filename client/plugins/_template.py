"""插件模板。复制本文件并去掉文件名开头的下划线即可生效
（以下划线开头的文件不会被加载）。

四种插件，按需选择一种或几种，用 @register 注册：

  Collector  每个采样周期产出指标值
  Source     持续推送数据（指标或事件），例如读日志、接收 SDK 上报
  Hook       测试开始/结束时执行，例如发通知、触发其他系统
  Platform   新平台（如 iOS），需要同时实现 Device

完整说明见仓库根目录的 PLUGINS.md。
"""
from perfclient.plugin import Collector, Hook, MetricDef, RateTracker, Source, register


# @register
class MyCollector(Collector):
    name = "my.collector"               # 全局唯一；与内置同名会覆盖内置实现
    platforms = ("android",)
    metrics = [MetricDef("my_metric", "我的指标", "ms", better="lower", order=200)]
    needs_process = True                # App 未运行时是否跳过

    def __init__(self, ctx, enabled):
        super().__init__(ctx, enabled)
        self.rates = RateTracker()      # 处理累计计数器 -> 每秒速率

    def files(self, pid):
        return []                       # 需要读的文件，会合并为一次读取

    def sample(self, tick):
        # tick.ts 时间戳(ms)  tick.pid 进程号  tick.files 上面声明的文件内容
        # self.ctx.device.shell("...") 执行命令；self.ctx.plugin_config(self.name) 读配置
        return {"my_metric": 0}

    def reset(self):
        self.rates.reset()              # App 进程重启时调用


# @register
class MySource(Source):
    name = "my.source"
    label = "我的数据源"
    platforms = ("android",)
    default_enabled = False

    def start(self):
        # 启动线程，拿到数据后调用：
        #   self.emit_metric(ts_ms, "biz.xxx", value)
        #   self.emit_event(ts_ms, "marker", "标记内容")
        pass

    def stop(self):
        pass


# @register
class MyHook(Hook):
    name = "my.hook"

    def on_session_start(self, session):
        pass

    def on_session_end(self, session, store):
        # store.points(session["id"]) / store.events(session["id"]) 可读取本次数据
        pass

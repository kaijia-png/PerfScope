# 插件开发指南

PerfScope 的核心只负责调度：按间隔采样、记录数据、检测进程重启、判断告警、存储和上传。**具体采什么、从哪采、结束后做什么，全部由插件决定。** 内置的 Android 和 iOS 支持本身也是用同一套插件接口实现的（`client/perfclient/platforms/android/__init__.py`、`client/perfclient/platforms/ios/__init__.py`），写插件时可以直接参考。

新增插件不需要修改任何核心代码，服务端也不需要改动：指标的显示名、单位、优劣方向会随数据一起上传。

## 插件放在哪里

客户端启动时按顺序加载，后加载的同名插件会覆盖先加载的：

1. 内置平台（`perfclient/platforms/`）
2. Python 包的 entry point，组名 `perfscope.plugins`（适合打包分发）
3. `client/plugins/*.py`（随代码仓库提交，团队共享）
4. `~/.perfscope/plugins/*.py`（个人使用）
5. 环境变量 `PERFSCOPE_PLUGIN_DIRS` 指定的目录（多个目录用系统路径分隔符隔开）

文件名以 `_` 开头的不会被加载，`client/plugins/_template.py` 就是一个模板。某个插件加载失败不会影响其他插件和工具本身，错误会显示在客户端的「插件」页，也可以用命令查看：

```bash
python -m perfclient plugins
```

## 四种插件

| 类型 | 用途 | 例子 |
| --- | --- | --- |
| `Collector` 采集器 | 每个采样周期产出指标值 | CPU、内存、线程数、文件句柄数 |
| `Source` 数据源 | 持续推送数据，不受采样周期限制 | logcat 业务日志、埋点 SDK 上报、抓包 |
| `Hook` 钩子 | 测试开始或结束时执行 | 飞书通知、自动截图、清理数据 |
| `Platform` 平台 | 发现设备、和设备通信 | Android、iOS（内置） |

全部从 `perfclient.plugin` 导入，用 `@register` 注册。

## 采集器

最常见的扩展。下面是 `client/plugins/threads.py` 的完整内容：

```python
from perfclient.plugin import Collector, MetricDef, register
from perfclient.platforms.android.parsers import parse_status_field


@register
class ThreadsCollector(Collector):
    name = "android.threads"
    platforms = ("android",)
    metrics = [MetricDef("threads", "线程数", "个", "lower", order=25)]

    def files(self, pid):
        return [f"/proc/{pid}/status"]

    def sample(self, tick):
        n = parse_status_field(tick.files.get(f"/proc/{tick.pid}/status", ""), "Threads")
        return {"threads": n} if n is not None else {}
```

几个要点：

- **`MetricDef(key, label, unit, better, order)`**：`better` 是 `"lower"` 或 `"higher"`，对比页据此判断变好还是变差；`order` 决定显示顺序，内置指标在 10 到 52 之间，业务指标默认 1000。
- **`files(pid)`** 声明需要读的文件。所有采集器声明的文件会合并成**一次** adb 调用读取，结果放在 `tick.files` 里。能用读文件解决的尽量用这种方式，采样开销最小。
- 需要执行命令时用 `self.ctx.device.shell("...")`，每条命令是一次单独的 adb 调用。
- **计数器类数据**（例如累计流量、累计帧数）用 `RateTracker` 换算成每秒速率，计数器归零的情况已经处理好。
- App 进程重启时会调用 `reset()`，用于清掉和进程相关的状态。
- `needs_process = False` 表示 App 没运行时也继续采集，适用于电量、温度这类整机指标。
- `sample()` 可以返回多余的指标，只有用户启用的才会被记录。
- 采集器抛出异常时，核心会记录一条错误事件（同一个采集器恢复正常前只记录一次），**其他采集器不受影响**。

**覆盖内置实现**：注册一个同名采集器即可。例如某个机型的电流文件路径不同，可以写一个 `name = "android.current"` 的插件替换内置版本，或者直接在配置里改路径：

```yaml
plugins:
  android.current: {path: /sys/class/power_supply/bms/current_now}
```

插件通过 `self.ctx.plugin_config(self.name)` 读取自己的配置。

## 数据源

适合"有数据就推送"的场景。拿到数据后调用两个回调：

```python
self.emit_metric(ts_ms, "biz.upload_speed", 1234.0)   # 指标；业务指标建议以 biz. 开头
self.emit_event(ts_ms, "marker", "开始上传")           # 事件；marker 会在所有曲线上画竖线
```

`start()` 里启动自己的线程，`stop()` 里清理。`default_enabled` 决定界面上默认是否勾选。参考内置的 `LogcatSource`。

## 钩子

```python
from perfclient.plugin import Hook, register

@register
class MyHook(Hook):
    name = "my.hook"

    def on_session_start(self, session):
        ...

    def on_session_end(self, session, store):
        points = store.points(session["id"])   # [[ts, metric, value], ...]
        events = store.events(session["id"])   # [[ts, type, label], ...]
```

钩子默认对所有测试生效。如果需要按测试开关，读 `session["config"]["plugins"]` 里自己的配置，参考 `client/plugins/feishu_notify.py`。

## 平台

iOS 平台没有可以直接读的文件，`IOSAgent`（`platforms/ios/agent.py`）和手机保持一条连接，在后台持续接收 Instruments 的数据流，iOS 采集器每次取最新值即可（见 `IOSSysmonCollector`）。给 iOS 写采集器时可以用 `self.ctx.device.latest(名称, 协程函数)` 复用这个机制。

实现 `Platform`（`list_devices`、`device`）和 `Device`（`info`、`list_apps`、`app_version`、`pid`、`read_files`、`shell`），再为这个平台写采集器即可。界面、存储、上传、服务端都不需要改。`tests/test_plugins.py` 里有一个不需要真实设备的最小平台实现，可以作为起点。

## 业务指标的显示名

日志里的指标名是 `asr_latency` 这样的英文标识，显示名和单位在测试配置里设置，会随数据上传：

```yaml
metric_meta:
  biz.asr_latency: {label: 转写延迟, unit: ms}
  biz.upload_speed: {label: 上传速度, unit: KB/s, better: higher}
```

客户端界面的「业务指标显示名」输入框做的是同一件事。

## 服务端插件

服务端也有钩子，在后台线程运行，执行慢或失败都不会影响上传：

```python
from perfserver.hooks import register

@register
class LeakAlarm:
    def on_session_uploaded(self, session):   # 完整摘要，含 stats 和 events
        ...
    def on_session_deleted(self, session_id):
        ...
```

放在 `server/plugins/*.py`、`PERFSCOPE_SERVER_PLUGIN_DIRS` 指定的目录，或注册为 entry point（组名 `perfscope.server_plugins`）。

内置一个 Webhook 钩子：设置 `WEBHOOK_URLS`（多个用逗号分隔）后，每次上传和删除都会 POST 一条 JSON，设置 `PUBLIC_URL` 后会附带报告链接。以后接入公司工具平台时，可以直接用它。

## 测试插件

```bash
python -m pytest tests
```

`tests/test_plugins.py` 展示了如何不连手机测试插件：注册一个测试平台，运行一次真实的会话，检查记录下来的数据。

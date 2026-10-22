# PerfScope

性能采集与分析工具，支持 Android 和 iOS 真机：客户端连接手机采集系统指标和业务日志指标，测试数据上传到服务器，团队在网页上查看、对比、看趋势。

```
client/
  perfclient/plugin.py       插件接口（扩展只需要看这一个文件）
  perfclient/platforms/      内置平台，Android 也是用插件接口实现的
  plugins/                   放在这里的插件自动加载（含示例：线程数、飞书通知）
server/
  perfserver/hooks.py        服务端插件接口，内置 Webhook
  plugins/                   服务端插件目录
tools/fake_adb.py            模拟一台 Android 设备，用于演示和开发
tests/                       单元测试，包括插件机制的端到端测试
```

工具本身只负责调度：采样、存储、告警、上传。**采什么、从哪采、测试结束后做什么，都由插件决定**，新增指标不需要改核心代码，也不需要改服务端。详见 [PLUGINS.md](PLUGINS.md)。

## 快速开始

只需要先安装 **Python 3.10 或更新版本**（Windows 安装时勾选 "Add python.exe to PATH"）。脚本会自动创建虚拟环境、安装依赖、启动服务端和客户端，并打开浏览器。

| 系统 | 连接真实手机 | 没有手机，先用模拟设备体验 |
| --- | --- | --- |
| Windows | 双击 `start.bat` | 在命令行运行 `start.bat --demo` |
| macOS | 双击 `PerfScope.command`（或 `./start.sh`） | `./start.sh --demo` |
| Linux | `./start.sh` | `./start.sh --demo` |

第一次运行需要下载依赖，大约一两分钟。启动后：

- 客户端：http://127.0.0.1:8765 （采集界面）
- 服务端：http://127.0.0.1:9000 （测试记录、对比、趋势）

客户端已自动填好服务器地址，用户名默认是电脑的登录名，可以在「服务器设置」里修改。数据保存在项目目录下的 `data/` 文件夹。

**连接真实手机**还需要：

1. 安装 adb（Android platform-tools）。macOS 可以用 `brew install android-platform-tools`；Windows 从 [官网](https://developer.android.com/tools/releases/platform-tools) 下载解压，把目录加入 PATH，或设置环境变量 `ADB_PATH` 指向 `adb.exe`。
2. 手机打开开发者选项和 USB 调试，用数据线连接电脑，在手机上点「允许 USB 调试」。
3. 在客户端点「刷新」，就能看到设备。

**连接 iPhone**（不需要 Xcode，也不需要 sudo）：

1. 用数据线连接电脑，解锁 iPhone，点「信任此电脑」并输入锁屏密码。Windows 需要先安装 Apple 设备支持（安装 iTunes 或 Apple Devices）。
2. 打开开发者模式：「设置 → 隐私与安全性 → 开发者模式」，打开后手机会重启，重启后再确认一次。看不到这个选项时，连着电脑运行一次 `.venv/bin/pymobiledevice3 amfi reveal-developer-mode`，它就会出现。
3. 在客户端点「刷新」，选择 iPhone 和要测的 App。第一次开始测试时会自动挂载开发者磁盘镜像并建立连接，需要十几秒到半分钟。

iOS 17.0–17.3 在 Windows 上需要先以管理员身份运行 `pymobiledevice3 remote tunneld`；macOS 和 iOS 17.4 及以上不需要。

**端口被占用**时，脚本会提示并退出。可以换端口启动：macOS/Linux 用 `SERVER_PORT=9001 CLIENT_PORT=8766 ./start.sh`；Windows 先执行 `set SERVER_PORT=9001`，再运行 `start.bat`。

**临时给同事试用**：`SHARE=1 ./start.sh`，服务端会监听局域网，启动时打印出 `http://<本机 IP>:9000`，发给同一网络的同事即可打开。这种方式没有登录认证，电脑休眠或关机后同事就打不开了，只适合短期试用。

**给团队共用**：把服务端部署到一台大家都能访问的机器上（见下文「服务端部署」），每个人在自己电脑上只运行客户端（`cd client && python -m perfclient serve`），在「服务器设置」里填写共用服务器的地址即可。

## 采集了什么

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| App CPU | `/proc/<pid>/stat` | 占整机 CPU 的百分比 |
| App 内存 (PSS) | `dumpsys meminfo` | 长时间录音时重点看「每小时变化」，持续为正通常是泄漏 |
| FPS | `dumpsys gfxinfo` | 只统计实际渲染的帧，界面静止时 FPS 为 0 属正常 |
| Jank / BigJank / 卡顿率 | `dumpsys gfxinfo framestats` | PerfDog 口径：某帧超过前 3 帧平均的 2 倍且超过 83.3 ms 为 Jank，超过 125 ms 为 BigJank；卡顿率 = Jank 帧耗时占比。采样间隔需不超过 2 秒 |
| 冷启动耗时 | `am start -S -W` | 勾选「开始前测 3 次冷启动耗时」后，测试开始前先关闭 App 再启动 3 次 |
| 上下行流量 | `/proc/net/dev` | 目前是**整机**流量，测试时尽量关掉其他应用 |
| 电量 / 电池温度 | `dumpsys battery` | |
| 电流 | `/sys/class/power_supply/battery/current_now` | 部分机型不提供，或单位不同；路径可在配置中修改 |
| 线程数 | `/proc/<pid>/status` | 由示例插件 `client/plugins/threads.py` 提供 |

iOS 通过 Instruments 服务采集（和 Xcode 用的是同一套数据）：

| 指标 | 说明 |
| --- | --- |
| App CPU | 已按核心数折算成占整机的百分比，和 Android 口径一致（Xcode 里显示的是单核百分比，可能超过 100%） |
| App 内存 (Footprint) | 和 Xcode 内存仪表、系统内存上限判断用的是同一个值；和 Android 的 PSS 不是同一指标，所以用了不同的名字 |
| 线程数 | |
| FPS(整屏) | 整个屏幕的 Core Animation 帧率，不区分 App；界面静止时为 0 属正常。iOS 没有卡顿帧计数 |
| 上下行流量 | **只统计被测 App**，比 Android 的整机流量更准 |
| 电量 / 电池温度 / 电流 | 来自电池管理芯片，约 5 秒更新一次 |

运行 `python -m perfclient plugins` 可以查看当前安装了哪些采集器和指标。

**截图**：开始测试前勾选「每秒截一张屏幕」（默认勾选），测试中每秒截一张手机屏幕，显示在曲线上方。鼠标移到任意曲线上，就会显示那一刻的画面，方便对上「内存突然上涨时在哪个页面」。截图会缩小成宽 360 像素的 JPEG，每张约 4–40KB，随测试数据一起上传，服务端的测试详情页也能这样查看。截图在单独的线程里进行，不会拖慢指标采样；部分手机截一张图要将近 1 秒，实际频率可能略低于每秒一张。配置文件中写 `screenshots: {interval: 1}` 开启。

**崩溃日志**：测试中 App 闪退或被系统杀掉时，自动抓取日志挂到这次测试上，在事件里点「查看日志」即可查看和复制。Android 读取系统的崩溃日志（Java 异常和 Native 崩溃），并识别 ANR；iOS 读取系统崩溃报告，包括闪退、内存超限被终止（Jetsam）和异常报告。日志会随测试一起上传。

会话中还会自动记录这些事件：进程重启、进程退出、告警触发、手动或 App 打的标记。

## 业务指标：让 App 打日志

系统层拿不到转写延迟、Agent 首字时间这类数据。只需要在**测试包**里按约定格式打日志，客户端会自动读取：

```kotlin
// 指标：PERF|名称|数值
Log.i("Perf", "PERF|asr_latency|${latencyMs}")
Log.i("Perf", "PERF|agent_ttft|${ttftMs}")

// 标记：PERF_MARK|内容，会在所有曲线上画一条竖线
Log.i("Perf", "PERF_MARK|开始录音")
```

iOS 用 `NSLog` 或 `os_log` 打同样格式的日志。注意 `os_log` 默认会把动态内容显示成 `<private>`，需要写成 `%{public}`：

```swift
NSLog("PERF|asr_latency|%d", latencyMs)
os_log("PERF|agent_ttft|%{public}d", log: perfLog, type: .info, ttftMs)
os_log("PERF_MARK|%{public}@", log: perfLog, type: .info, "开始录音")
```

指标在系统里叫 `biz.asr_latency`、`biz.agent_ttft`，可以和其他指标一样设置告警、对比、看趋势。在配置的 `metric_meta` 里（或客户端界面的「业务指标显示名」）可以给它们设置中文名和单位。如果不想改日志格式，也可以在配置里写自己的正则（见下文 `log_rules`）。

## 页面加载时长

每个页面单独成为一个指标（如「页面加载：录音详情」），可以看曲线、做对比、看趋势；测试详情页还有一张按页面汇总的表（打开次数、平均、P90、最慢）。两种来源：

- **Android 自动采集**：系统每次打开一个页面（Activity）都会记录耗时（`Displayed …: +350ms`），不用改 App。App 调用 `reportFullyDrawn()` 时，还会记录「完全绘制」的耗时。只有一个 Activity、靠 Fragment / Compose 切换页面的 App，只能采到首次打开的那一次。
- **App 打日志**（Android 和 iOS 通用）：页面可以使用时打一行 `PERF_PAGE|页面名|耗时毫秒`。

```kotlin
Log.i("Perf", "PERF_PAGE|录音详情|$costMs")
```
```swift
NSLog("PERF_PAGE|录音详情|%d", costMs)
```

## 三种用法

**界面操作**：选设备和应用，点「开始测试」，过程中随时打标记，结束后上传。适合探索性测试。

**配置文件**：把一次测试的设置写成 YAML，反复执行。示例见 `client/configs/example.yaml`。

```bash
python -m perfclient run configs/example.yaml --upload --server http://perfscope.internal:9000
```

**接入 CI**：同上命令，在配置里设置 `fail_on_alert: true`，触发告警时进程以非 0 退出，流水线会失败。

配置项说明：

```yaml
name: 录音中切后台 2h
project: 录音App-Android        # 服务器上的项目，不存在会自动创建
tags: [录音, 后台, 回归]          # 用于筛选和趋势查询
target: {platform: android, package: com.example.recorder, serial: 可选}
metrics: [cpu, mem_pss, fps]    # 不写则采集全部已安装的指标
interval: 1                     # 采样间隔，秒
duration: 2h                    # 2h / 30m / 90s；不写则手动结束
sources: [android.logcat]       # 启用哪些数据源；不写则用各数据源的默认设置
metric_meta:                    # 业务指标的显示名和单位
  biz.asr_latency: {label: 转写延迟, unit: ms}
plugins:                        # 传给各插件的配置
  android.current: {path: /sys/class/power_supply/battery/current_now}
log_rules:                      # 不写则使用 PERF|名称|数值 格式
  - pattern: 'PERF\|(?P<name>[\w.\-]+)\|(?P<value>-?\d+(?:\.\d+)?)'
  - name: upload_ms             # 固定名称 + 只取数值
    pattern: 'upload done in (?P<value>\d+)ms'
alerts:                         # 进入超限状态时记一次告警
  mem_pss: "> 800"
  biz.agent_ttft: "> 3000"
upload: true
fail_on_alert: false
```

## 基准和对比

工具不规定什么是基准。任意一次历史测试都可以作为参照：在服务器「测试记录」页勾选多条，第一条勾选的就是参照。对比页可以选比较的统计值（平均、P90、P99、最大、每小时变化）和差异阈值，变差超过阈值的标红。参与对比的测试如果设备、系统版本或应用不一致，页面顶部会提示。

建议团队用标签区分测试方案（例如 `录音2h基准`），在「趋势」页按标签筛选，就能看到同一方案在各版本的变化。

## 服务端部署

```bash
docker compose up -d --build
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./perfscope.db` | 生产建议 `mysql+pymysql://...`（MySQL/TiDB）或 PostgreSQL |
| `BLOB_DIR` | `./blobs` | 原始采样数据目录 |
| `S3_BUCKET` / `S3_PREFIX` / `S3_ENDPOINT` | 空 | 设置后原始数据改存 S3 |
| `AUTH_MODE` | `none` | `none`：信任客户端上报的用户名；`token`：按令牌认证 |
| `AUTH_TOKENS` | 空 | `token:用户名,token:用户名` |
| `WEBHOOK_URLS` | 空 | 上传、删除测试记录时 POST 通知到这些地址（逗号分隔） |
| `PUBLIC_URL` | 空 | 服务器对外地址，用于在 Webhook 中附带报告链接 |

数据存储方式：会话信息、统计值、事件存数据库；原始采样数据按会话压缩后存文件或 S3。列表、对比、趋势只查数据库，打开单次会话详情时才读原始数据。

## 以后集成到工具平台

已经预留的位置：

- 登录只在 `server/perfserver/auth.py` 的 `get_user()` 里处理，换成平台 SSO 只改这一处
- 项目表有 `external_id` 字段，用于映射平台的项目
- Web 页面只通过 `/api/*` 读取数据，接口文档在 `http://<服务器>:9000/docs`
- 内置 Webhook（`WEBHOOK_URLS`），平台可以订阅测试上传事件；需要更深的联动时写服务端插件
- 服务端是无状态容器，配置全部来自环境变量

## 已知限制

- 支持 Android 和 iOS 真机；iOS 模拟器、鸿蒙等其他平台可以按 PLUGINS.md 以插件形式加入
- iOS 没有卡顿指标，也不能自动测冷启动（需要 App 打 `PERF|launch_ms|耗时` 日志上报）
- iOS 的 FPS 是整屏帧率，没有卡顿帧；需要 App 级帧率时可以让 App 自己打 `PERF|fps|...` 日志
- Android 的网络流量是整机数据，不是单个应用（iOS 是单个应用）
- 电流读数依赖机型，部分设备没有或单位异常
- 客户端界面目前在浏览器中打开，后续用 Electron/Tauri 打包成桌面应用（界面代码可直接复用）
- 页面图表从 jsdelivr CDN 加载 ECharts，完全离线的内网需要改成本地文件

## 开发

```bash
pip install pytest
python -m pytest tests
```

客户端接口文档：启动后访问 `http://127.0.0.1:8765/docs`。

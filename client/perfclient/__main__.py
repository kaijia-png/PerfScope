"""
python -m perfclient serve [--port 8765]          打开客户端界面
python -m perfclient devices                      列出设备
python -m perfclient plugins                      查看已安装的插件和可用指标
python -m perfclient run config.yaml [--upload]   按配置无界面执行（适合 CI）
"""
import argparse
import json
import signal
import sys
import threading
import webbrowser


def main():
    p = argparse.ArgumentParser(prog="perfclient")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("devices")
    sub.add_parser("plugins")
    r = sub.add_parser("run")
    r.add_argument("config")
    r.add_argument("--serial", help="覆盖配置里的设备")
    r.add_argument("--upload", action="store_true", help="结束后上传到服务器")
    r.add_argument("--server", help="服务器地址（默认用客户端设置）")
    args = p.parse_args()

    if args.cmd == "devices":
        from . import plugin
        plugin.load_plugins()
        for p in plugin.platforms():
            print(json.dumps(p.list_devices(), ensure_ascii=False, indent=2))
    elif args.cmd == "plugins":
        print_plugins()
    elif args.cmd == "run":
        run_headless(args)
    else:
        import uvicorn
        port = getattr(args, "port", 8765)
        if not getattr(args, "no_browser", False):
            threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        uvicorn.run("perfclient.app:app", host="127.0.0.1", port=port, log_level="warning")


def print_plugins():
    from . import plugin
    plugin.load_plugins()
    d = plugin.describe()
    for kind, title in (("platforms", "平台"), ("collectors", "采集器"), ("sources", "数据源"),
                        ("hooks", "钩子")):
        print(f"{title}:")
        for item in d[kind] or [{"name": "（无）", "origin": "", "metrics": []}]:
            extra = f"  指标: {', '.join(item['metrics'])}" if item.get("metrics") else ""
            origin = "" if item["origin"] in ("builtin", "") else f"  [{item['origin']}]"
            print(f"  - {item['name']}{extra}{origin}")
    for e in d["errors"]:
        print(f"加载失败: {e['origin']}\n{e['error']}")


def run_headless(args):
    import yaml
    from . import plugin
    from .session import SessionRunner
    from .storage import Store
    from .uploader import upload

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("target", {})
    if args.serial:
        cfg["target"]["serial"] = args.serial
    if not cfg["target"].get("serial"):
        plugin.load_plugins()
        platform = plugin.get_platform(cfg["target"].get("platform", "android"))
        online = [d for d in platform.list_devices() if d["state"] == "device"]
        if len(online) != 1:
            sys.exit(f"请用 --serial 指定设备（当前在线 {len(online)} 台）")
        cfg["target"]["serial"] = online[0]["serial"]

    store = Store()
    runner = SessionRunner(store, cfg)
    runner.start()
    print(f"开始测试 {runner.id}，Ctrl+C 结束")
    signal.signal(signal.SIGINT, lambda *_: runner.stop())
    runner.wait()
    runner.stop()
    alerts = [e for e in store.events(runner.id) if e[1] == "alert"]
    print(f"测试结束，采样点 {len(store.points(runner.id))} 个，告警 {len(alerts)} 次")

    if args.upload or cfg.get("upload"):
        import os
        st = store.get_settings()
        server = args.server or st.get("server_url") or os.environ.get("PERFSCOPE_SERVER_URL")
        rid = upload(store, runner.id, server, st.get("token"),
                     st.get("user") or os.environ.get("PERFSCOPE_USER"))
        print(f"已上传: {server.rstrip('/')}/#/s/{rid}")
    # Non-zero exit when alerts fired, so CI can fail the job
    sys.exit(1 if alerts and cfg.get("fail_on_alert") else 0)


if __name__ == "__main__":
    main()

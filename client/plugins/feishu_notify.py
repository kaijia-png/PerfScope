"""示例钩子插件：测试结束后发飞书群通知。

设置环境变量 PERFSCOPE_FEISHU_WEBHOOK=<飞书群机器人 webhook 地址> 后生效，
没设置时什么也不做。也可以在单次测试配置里关闭：
    plugins:
      feishu_notify: {enabled: false}
"""
import os

import requests

from perfclient.plugin import Hook, register


@register
class FeishuNotify(Hook):
    name = "feishu_notify"

    def on_session_end(self, session, store):
        url = os.environ.get("PERFSCOPE_FEISHU_WEBHOOK")
        cfg = ((session.get("config") or {}).get("plugins") or {}).get(self.name) or {}
        if not url or cfg.get("enabled") is False:
            return
        alerts = [e for e in store.events(session["id"]) if e[1] == "alert"]
        crashes = [e for e in store.events(session["id"]) if e[1].startswith("process_")]
        minutes = round((session["ended_at"] - session["started_at"]) / 60000, 1)
        text = (f"性能测试结束：{session['name']}\n"
                f"应用 {session['app_package']} {session.get('app_version', '')}，时长 {minutes} 分钟\n"
                f"告警 {len(alerts)} 次，进程异常 {len(crashes)} 次")
        requests.post(url, json={"msg_type": "text", "content": {"text": text}}, timeout=10)

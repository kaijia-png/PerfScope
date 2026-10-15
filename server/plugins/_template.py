"""服务端插件模板。复制并去掉文件名开头的下划线即可生效。

服务端钩子在后台线程运行，失败不会影响上传。可用的方法（都可选）：
    on_session_uploaded(session)   上传完成后；session 是完整的会话摘要（含统计值和事件）
    on_session_deleted(session_id)
"""
from perfserver.hooks import register


# @register
class MyServerHook:
    def on_session_uploaded(self, session):
        # 例：内存每小时增长超过 50MB 时通知
        mem = session["stats"].get("mem_pss") or {}
        if (mem.get("slope_per_hour") or 0) > 50:
            print(f"疑似内存泄漏: {session['name']}")

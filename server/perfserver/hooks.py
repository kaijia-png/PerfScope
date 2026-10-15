"""Server-side extension points.

A hook is any object with some of these methods; all are optional:

    on_session_uploaded(session: dict)   after a session is stored (session = full summary)
    on_session_deleted(session_id: str)

Register with @register. Hooks run in a background thread, so a slow or failing
hook never delays or breaks an upload. Plugins are loaded from:
    - Python entry points in group "perfscope.server_plugins"
    - *.py files in server/plugins/ and $PERFSCOPE_SERVER_PLUGIN_DIRS

Built in: WebhookHook, enabled by WEBHOOK_URLS (comma separated). This is the
intended integration point for the company tool platform.
"""
import importlib.util
import json
import logging
import os
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

log = logging.getLogger("perfscope.hooks")
_HOOKS = []
LOAD_ERRORS = []


def register(cls):
    _HOOKS.append(cls())
    return cls


def emit(event: str, *args):
    def run():
        for h in _HOOKS:
            fn = getattr(h, event, None)
            if fn:
                try:
                    fn(*args)
                except Exception:
                    log.exception("hook %s.%s failed", type(h).__name__, event)
    threading.Thread(target=run, daemon=True).start()


@register
class WebhookHook:
    """POSTs JSON to every URL in WEBHOOK_URLS.
    Body: {"event": "session.uploaded", "session": {...}, "url": "<link to report>"}"""

    def __init__(self):
        self.urls = [u.strip() for u in os.environ.get("WEBHOOK_URLS", "").split(",") if u.strip()]
        self.public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")

    def _post(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        for u in self.urls:
            req = urllib.request.Request(u, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()

    def on_session_uploaded(self, session):
        if self.urls:
            self._post({"event": "session.uploaded", "session": session,
                        "url": f"{self.public_url}/#/s/{session['id']}" if self.public_url else None})

    def on_session_deleted(self, session_id):
        if self.urls:
            self._post({"event": "session.deleted", "session_id": session_id})


def load_plugins():
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="perfscope.server_plugins"):
            try:
                ep.load()
            except Exception as e:
                LOAD_ERRORS.append({"origin": ep.name, "error": str(e)})
    except Exception:
        pass
    dirs = [Path(__file__).resolve().parents[1] / "plugins"]
    dirs += [Path(d) for d in os.environ.get("PERFSCOPE_SERVER_PLUGIN_DIRS", "").split(os.pathsep) if d]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"perfscope_server_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
            except Exception as e:
                LOAD_ERRORS.append({"origin": str(f), "error": f"{e}\n{traceback.format_exc(limit=3)}"})


def describe():
    return {"hooks": [type(h).__name__ for h in _HOOKS], "errors": LOAD_ERRORS}

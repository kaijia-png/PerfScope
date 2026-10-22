"""Local API for the client UI (and for a future Electron/Tauri shell)."""
import getpass
import os
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import plugin
from .session import SessionRunner
from .storage import Store
from .uploader import upload

STATIC = Path(__file__).parent / "static"

plugin.load_plugins()
app = FastAPI(title="PerfScope Client")
store = Store()
state = {"runner": None}


def err(e):
    raise HTTPException(400, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/plugins")
def plugins():
    return plugin.describe()


@app.get("/api/metrics")
def metrics(platform: str = "android"):
    defs = plugin.metric_defs(platform)
    return {"metrics": {k: d.to_dict() for k, d in defs.items()},
            "default": list(defs),
            "sources": [{"name": s.name, "label": s.label or s.name,
                         "default_enabled": s.default_enabled}
                        for s in plugin.sources(platform)]}


@app.get("/api/devices")
def devices():
    out, errors = [], []
    for p in plugin.platforms():
        try:
            out += [{**d, "platform": p.name} for d in p.list_devices()]
        except Exception as e:
            errors.append(f"{p.label or p.name}: {e}")
    if not out and errors:
        err("；".join(errors))
    return out


@app.get("/api/devices/{platform_name}/{serial}/apps")
def apps(platform_name: str, serial: str):
    try:
        return plugin.get_platform(platform_name).device(serial).list_apps()
    except Exception as e:
        err(e)


class StartReq(BaseModel):
    platform: str = "android"
    serial: str
    package: str
    name: str = ""
    project: str = "default"
    tags: list[str] = []
    env: str = ""
    notes: str = ""
    metrics: Optional[list[str]] = None
    interval: float = 1.0
    duration: Optional[str] = None
    sources: Optional[list[str]] = None
    log_rules: Optional[list[dict]] = None
    alerts: dict = {}
    metric_meta: dict = {}
    plugins: dict = {}
    screenshots: Union[bool, dict, None] = None
    launch: Optional[dict] = None


@app.post("/api/run/start")
def start(req: StartReq):
    r = state["runner"]
    if r and r.status == "running":
        err("已有测试在进行，请先结束")
    cfg = req.model_dump()
    cfg["target"] = {"platform": cfg.pop("platform"), "serial": cfg.pop("serial"),
                     "package": cfg.pop("package")}
    try:
        runner = SessionRunner(store, cfg)
        runner.start()
    except Exception as e:
        err(e)
    state["runner"] = runner
    return runner.snapshot()


@app.post("/api/run/stop")
def stop():
    r = state["runner"]
    if not r:
        err("没有进行中的测试")
    r.stop()
    return r.snapshot()


class MarkerReq(BaseModel):
    label: str
    ts: Optional[int] = None   # epoch ms of the click / first keystroke; default: now


@app.post("/api/run/marker")
def marker(req: MarkerReq):
    r = state["runner"]
    if not r or r.status != "running":
        err("没有进行中的测试")
    ts = r.add_marker(req.label.strip() or "标记", req.ts)
    return {"ok": True, "ts": ts}


class NameReq(BaseModel):
    name: str


@app.post("/api/sessions/{sid}/rename")
def rename_session(sid: str, req: NameReq):
    name = " ".join(req.name.split())[:100]
    if not name:
        err("测试名称不能为空")
    if not store.rename_session(sid, name):
        err("找不到这条记录")
    return {"ok": True, "name": name, **_sync_name(sid, name)}


def _sync_name(sid, name):
    """An uploaded record is renamed on the server too, so nobody has to re-upload for a new name."""
    s = store.get_session(sid)
    st = get_settings()
    if not (s and s.get("remote_id") and st["server_url"]):
        return {"synced": None}
    from urllib.parse import quote
    import requests
    headers = {"X-User": quote(st["user"])} if st["user"] else {}
    if st["token"]:
        headers["Authorization"] = f"Bearer {st['token']}"
    try:
        r = requests.patch(f"{st['server_url'].rstrip('/')}/api/sessions/{quote(s['remote_id'])}",
                           json={"name": name}, headers=headers, timeout=8)
        if r.status_code < 400:
            return {"synced": True}
        return {"synced": False, "warning": f"服务器上的名称没有同步（{r.status_code}），可以点「重新上传」"}
    except requests.RequestException:
        return {"synced": False, "warning": "连不上服务器，服务器上还是旧名称；连上后点「重新上传」即可"}


class RenameReq(BaseModel):
    ts: int
    old: str
    label: str


@app.post("/api/sessions/{sid}/markers/rename")
def rename_marker(sid: str, req: RenameReq):
    """Markers can be named after the fact: mark first, type the name later."""
    label = req.label.strip()
    if not label:
        err("标记名称不能为空")
    if not store.rename_event(sid, req.ts, "marker", req.old, label[:200]):
        err("找不到这个标记")
    return {"ok": True, "label": label[:200]}


def _meta_for(session, keys):
    cfg = session.get("config") or {}
    platform = cfg.get("platform", "android")
    return {k: plugin.metric_meta(k, platform, cfg.get("metric_meta")) for k in keys}


@app.get("/api/run/live")
def live(since: int = 0, since_shot: int = 0):
    r = state["runner"]
    if not r:
        return {"run": None}
    pts = store.points(r.id, since)
    return {"run": r.snapshot(), "points": pts, "events": store.events(r.id, since),
            "shots": store.shots(r.id, since_shot),
            "attachments": store.attachments(r.id),
            "metric_meta": _meta_for(store.get_session(r.id), {p[1] for p in pts})}


@app.get("/api/sessions")
def sessions():
    return store.list_sessions()


@app.get("/api/sessions/{sid}")
def session(sid: str):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    s["points"] = store.points(sid)
    s["events"] = store.events(sid)
    s["metric_meta"] = _meta_for(s, {p[1] for p in s["points"]})
    s["shots"] = store.shots(sid)
    s["attachments"] = store.attachments(sid)
    return s


@app.get("/api/sessions/{sid}/shots/{ts}")
def shot(sid: str, ts: int):
    p = store.shot_path(sid, ts)
    if not p.is_file():
        raise HTTPException(404, "截图不存在")
    # frames never change once written
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000, immutable"})


@app.delete("/api/sessions/{sid}")
def delete(sid: str):
    r = state["runner"]
    if r and r.id == sid and r.status == "running":
        err("测试进行中，不能删除")
    store.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/upload")
def do_upload(sid: str):
    st = get_settings()
    try:
        rid = upload(store, sid, st["server_url"], st["token"], st["user"])
    except (ValueError, RuntimeError) as e:
        err(e)
    return {"remote_id": rid, "url": st["server_url"].rstrip("/") + f"/#/s/{rid}"}


class Settings(BaseModel):
    server_url: str = ""
    token: str = ""
    user: str = ""
    auto_upload: bool = False


@app.get("/api/settings")
def get_settings():
    s = store.get_settings()
    try:
        default_user = getpass.getuser()
    except Exception:
        default_user = ""
    # Values saved in the UI win; environment (set by start scripts) fills the gaps
    return {"server_url": s.get("server_url") or os.environ.get("PERFSCOPE_SERVER_URL", ""),
            "token": s.get("token", ""),
            "user": s.get("user") or os.environ.get("PERFSCOPE_USER", default_user),
            "auto_upload": s.get("auto_upload") == "True"}


@app.put("/api/settings")
def put_settings(s: Settings):
    store.set_settings(s.model_dump())
    return get_settings()

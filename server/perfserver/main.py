"""PerfScope server: stores sessions, computes stats, serves the web viewer."""
import gzip
import io
import json
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from . import hooks
from . import stats as statmod
from .auth import can_modify, get_user
from .blob import make_blob
from .db import Event, MetricStat, Project, SessionLocal, TestSession, init_db

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="PerfScope Server")
blob = make_blob()
init_db()
hooks.load_plugins()

# Fallback display info for bundles from older clients that did not send metric_meta.
# New metrics never need to be added here: the client sends their meta with the data.
FALLBACK_META = {
    "cpu": {"label": "App CPU", "unit": "%", "better": "lower"},
    "mem_pss": {"label": "App 内存 (PSS)", "unit": "MB", "better": "lower"},
    "fps": {"label": "FPS", "unit": "帧/秒", "better": "higher"},
    "jank": {"label": "卡顿帧", "unit": "帧/秒", "better": "lower"},
    "net_rx": {"label": "下行流量(整机)", "unit": "KB/s", "better": "lower"},
    "net_tx": {"label": "上行流量(整机)", "unit": "KB/s", "better": "lower"},
    "battery_level": {"label": "电量", "unit": "%", "better": "higher", "min": 0, "max": 100},
    "battery_temp": {"label": "电池温度", "unit": "°C", "better": "lower"},
    "current": {"label": "电流", "unit": "mA", "better": "lower"},
}


DEFAULT_ORDER = {k: i * 10 for i, k in enumerate(FALLBACK_META)}


def metric_meta(m, session_meta=None):
    meta = (session_meta or {}).get(m)
    if meta:
        # sessions uploaded before axis ranges existed still get them for known metrics
        rng = {k: v for k, v in FALLBACK_META.get(m, {}).items() if k in ("min", "max")}
        return {"order": 500, "better": "lower", "unit": "", **rng, **meta}
    if m in FALLBACK_META:
        return {**FALLBACK_META[m], "order": DEFAULT_ORDER[m]}
    return {"label": m[4:] if m.startswith("biz.") else m, "unit": "", "better": "lower",
            "order": 1000}


def ordered(metrics, meta):
    return sorted(metrics, key=lambda m: (meta[m].get("order", 500), m))


def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def summary(s: TestSession, full=False):
    d = {
        "id": s.id, "name": s.name, "project": s.project.name if s.project else None,
        "platform": s.platform, "device_model": s.device_model, "os_version": s.os_version,
        "app_package": s.app_package, "app_version": s.app_version, "tags": s.tags or [],
        "started_at": s.started_at, "ended_at": s.ended_at, "duration_s": s.duration_s,
        "status": s.status, "created_by": s.created_by,
        "alert_count": sum(1 for e in s.events if e.type == "alert"),
        "crash_count": sum(1 for e in s.events if e.type in ("process_restart", "process_dead", "crash", "jetsam", "anr")),
    }
    if full:
        d.update({
            "notes": s.notes, "device": s.device, "config": s.config,
            "point_count": s.point_count,
            "stats": {st.metric: {k: getattr(st, k) for k in
                                  ("count", "avg", "min", "max", "p50", "p90", "p99",
                                   "first", "last", "slope_per_hour")} for st in s.stats},
            "events": [[e.ts, e.type, e.label] for e in s.events],
        })
        d["metric_meta"] = {m: metric_meta(m, s.metric_meta) for m in d["stats"]}
        d["metric_order"] = ordered(list(d["stats"]), d["metric_meta"])
    return d


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- client download
# Colleagues open this server in a browser and download a ready-to-run client from it. The package
# is built from the code next to the server, with server.txt pointing at the address they used, so it
# keeps working when this machine's IP changes (they just download again).
REPO = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", "data", "__pycache__", ".pytest_cache", ".git", "blobs", "dist"}
SKIP_FILES = {"server.txt", "run.log", ".DS_Store"}


def _client_files():
    for p in sorted(REPO.rglob("*")):
        rel = p.relative_to(REPO)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS) and p.name not in SKIP_FILES and p.suffix != ".db":
            yield p, rel


@app.get("/download/PerfScope.zip")
def download_client(request: Request):
    if not (REPO / "client" / "perfclient").is_dir():
        raise HTTPException(404, "这个服务端没有附带客户端代码")
    url = f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in _client_files():
            info = zipfile.ZipInfo.from_file(p, f"PerfScope/{rel.as_posix()}")   # keeps start.sh executable
            data = p.read_bytes()
            if rel.name == "START-HERE.txt":
                data = data.decode().replace("{{SERVER_URL}}", url).encode()
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(zipfile.ZipInfo("PerfScope/server.txt"), url + "\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="PerfScope.zip"', "Cache-Control": "no-store"})


@app.get("/api/me")
def me(user: str = Depends(get_user)):
    return {"user": user}


@app.post("/api/sessions/upload")
async def upload(file: UploadFile = File(...), user: str = Depends(get_user), d=Depends(db)):
    raw = await file.read()
    try:
        bundle = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        meta, points, events = bundle["session"], bundle["points"], bundle["events"]
    except (OSError, ValueError, KeyError) as e:
        raise HTTPException(400, f"上传内容格式不对: {e}")

    proj_name = (meta.get("project") or "default").strip()
    proj = d.scalar(select(Project).where(Project.name == proj_name))
    if not proj:
        proj = Project(name=proj_name)
        d.add(proj)
        d.flush()

    existing = d.get(TestSession, meta["id"])
    if existing:  # re-upload replaces the previous copy
        if not can_modify(user, existing.created_by):
            raise HTTPException(403, "这个会话由其他人上传，不能覆盖")
        d.delete(existing)
        d.flush()

    key = f"sessions/{meta['id']}.json.gz"
    blob.put(key, gzip.compress(json.dumps({"points": points}).encode()))
    # crash logs travel with the run; always rewrite so a re-upload without logs clears old ones
    blob.put(f"attachments/{meta['id']}.json.gz",
             gzip.compress(json.dumps(bundle.get("attachments") or [], ensure_ascii=False).encode()))
    dev = meta.get("device") or {}
    ended = meta.get("ended_at") or (points[-1][0] if points else meta["started_at"])
    s = TestSession(
        id=meta["id"], project_id=proj.id, name=meta.get("name"),
        platform=dev.get("platform"), device_model=" ".join(
            x for x in (dev.get("brand"), dev.get("model")) if x),
        os_version=dev.get("os_version"), app_package=meta.get("app_package"),
        app_version=meta.get("app_version"), tags=meta.get("tags") or [],
        notes=meta.get("notes") or "", device=dev, config=meta.get("config") or {},
        metric_meta=bundle.get("metric_meta") or {},
        started_at=meta["started_at"], ended_at=ended,
        duration_s=round((ended - meta["started_at"]) / 1000, 1),
        status=meta.get("status"), point_count=len(points), blob_key=key, created_by=user)
    for m, st in statmod.compute(points).items():
        s.stats.append(MetricStat(metric=m, **st))
    for ts, t, label in events:
        s.events.append(Event(ts=ts, type=t, label=(label or "")[:500]))
    d.add(s)
    d.commit()
    hooks.emit("on_session_uploaded", summary(s, full=True))
    return {"id": s.id}


@app.get("/api/projects")
def projects(d=Depends(db)):
    return [{"id": p.id, "name": p.name} for p in d.scalars(select(Project).order_by(Project.name))]


@app.get("/api/sessions")
def list_sessions(project: Optional[str] = None, app_package: Optional[str] = None,
                  app_version: Optional[str] = None, device_model: Optional[str] = None,
                  tag: Optional[str] = None, q: Optional[str] = None,
                  limit: int = 100, offset: int = 0, d=Depends(db)):
    stmt = select(TestSession).order_by(TestSession.started_at.desc())
    if project:
        stmt = stmt.join(Project).where(Project.name == project)
    for col, val in ((TestSession.app_package, app_package), (TestSession.app_version, app_version),
                     (TestSession.device_model, device_model)):
        if val:
            stmt = stmt.where(col == val)
    if q:
        stmt = stmt.where(TestSession.name.contains(q))
    rows = list(d.scalars(stmt))
    if tag:  # JSON filtering differs by DB; filter in Python to stay portable
        rows = [r for r in rows if tag in (r.tags or [])]
    return {"total": len(rows), "items": [summary(r) for r in rows[offset:offset + limit]]}


@app.get("/api/sessions/{sid}")
def get_session(sid: str, d=Depends(db)):
    s = d.get(TestSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    return summary(s, full=True)


@app.get("/api/sessions/{sid}/data")
def get_data(sid: str, d=Depends(db)):
    s = d.get(TestSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    return json.loads(gzip.decompress(blob.get(s.blob_key)))


# ---------------------------------------------------------------- screenshots
# One blob per frame (shots/<sid>/<ts>.jpg) so the viewer fetches only the frame it shows,
# plus shots/<sid>/index.json listing the timestamps.

def _shot_index(sid):
    try:
        return json.loads(blob.get(f"shots/{sid}/index.json"))
    except Exception:
        return []


def _delete_shots(sid):
    for ts in _shot_index(sid):
        blob.delete(f"shots/{sid}/{ts}.jpg")
    blob.delete(f"shots/{sid}/index.json")


@app.post("/api/sessions/{sid}/screenshots")
async def upload_shots(sid: str, reset: int = 0, file: UploadFile = File(...),
                       user: str = Depends(get_user), d=Depends(db)):
    s = d.get(TestSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在，请先上传测试数据")
    if not can_modify(user, s.created_by):
        raise HTTPException(403, "只有上传者可以上传截图")
    if reset:   # first batch of a (re-)upload replaces earlier frames
        _delete_shots(sid)
    index = set(_shot_index(sid))
    try:
        with zipfile.ZipFile(io.BytesIO(await file.read())) as z:
            for name in z.namelist():
                stem = name.rsplit("/", 1)[-1].removesuffix(".jpg")
                if stem.isdigit():
                    blob.put(f"shots/{sid}/{stem}.jpg", z.read(name))
                    index.add(int(stem))
    except zipfile.BadZipFile as e:
        raise HTTPException(400, f"截图包格式不对: {e}")
    blob.put(f"shots/{sid}/index.json", json.dumps(sorted(index)).encode())
    return {"count": len(index)}


@app.get("/api/sessions/{sid}/attachments")
def get_attachments(sid: str):
    """Crash logs of a run: [{ts, kind, title, text}]"""
    try:
        return json.loads(gzip.decompress(blob.get(f"attachments/{sid}.json.gz")))
    except Exception:
        return []


@app.get("/api/sessions/{sid}/screenshots")
def list_shots(sid: str):
    return _shot_index(sid)


@app.get("/api/sessions/{sid}/screenshots/{ts}")
def get_shot(sid: str, ts: int):
    try:
        data = blob.get(f"shots/{sid}/{ts}.jpg")
    except Exception:
        raise HTTPException(404, "截图不存在")
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=31536000, immutable"})


class Patch(BaseModel):
    name: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


@app.patch("/api/sessions/{sid}")
def patch_session(sid: str, p: Patch, user: str = Depends(get_user), d=Depends(db)):
    s = d.get(TestSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    if not can_modify(user, s.created_by):
        raise HTTPException(403, "只有上传者可以修改")
    for k, v in p.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    d.commit()
    return summary(s, full=True)


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, user: str = Depends(get_user), d=Depends(db)):
    s = d.get(TestSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    if not can_modify(user, s.created_by):
        raise HTTPException(403, "只有上传者可以删除")
    blob.delete(s.blob_key)
    blob.delete(f"attachments/{sid}.json.gz")
    _delete_shots(sid)
    d.delete(s)
    d.commit()
    hooks.emit("on_session_deleted", sid)
    return {"ok": True}


ENV_KEYS = [("platform", "平台"), ("device_model", "设备"), ("os_version", "系统版本"),
            ("app_package", "应用")]


@app.get("/api/compare")
def compare(ids: str = Query(..., description="逗号分隔，第一个为参照"), d=Depends(db)):
    id_list = [i for i in ids.split(",") if i]
    if len(id_list) < 2:
        raise HTTPException(400, "至少选择两次测试进行对比")
    sessions = []
    for i in id_list:
        s = d.get(TestSession, i)
        if not s:
            raise HTTPException(404, f"会话不存在: {i}")
        sessions.append(summary(s, full=True))
    ref = sessions[0]
    warnings = []
    for key, label in ENV_KEYS:
        vals = {s[key] for s in sessions}
        if len(vals) > 1:
            warnings.append(f"{label}不一致：{' / '.join(str(v) for v in vals)}")
    meta = {}
    for s in reversed(sessions):  # the reference session's meta wins
        meta.update(s["metric_meta"])
    metrics = ordered({m for s in sessions for m in s["stats"]}, meta)
    return {"reference": ref["id"], "sessions": sessions, "warnings": warnings,
            "metrics": metrics, "metric_meta": meta}


@app.get("/api/trend")
def trend(project: str, metric: str, stat: str = "avg", app_package: Optional[str] = None,
          tag: Optional[str] = None, d=Depends(db)):
    """Metric statistic across sessions over time, e.g. avg memory per release."""
    if stat not in ("avg", "min", "max", "p50", "p90", "p99", "slope_per_hour"):
        raise HTTPException(400, "stat 参数不支持")
    stmt = (select(TestSession).join(Project).where(Project.name == project)
            .order_by(TestSession.started_at))
    if app_package:
        stmt = stmt.where(TestSession.app_package == app_package)
    out, meta = [], None
    for s in d.scalars(stmt):
        if tag and tag not in (s.tags or []):
            continue
        st = next((x for x in s.stats if x.metric == metric), None)
        if st is not None:
            meta = metric_meta(metric, s.metric_meta)
            out.append({"id": s.id, "name": s.name, "app_version": s.app_version,
                        "started_at": s.started_at, "value": getattr(st, stat)})
    return {"metric": metric, "stat": stat, "meta": meta or metric_meta(metric), "points": out}


@app.get("/api/metrics")
def known_metrics(project: Optional[str] = None, d=Depends(db)):
    """Every metric that appears in stored sessions, for pickers in the UI."""
    stmt = select(TestSession)
    if project:
        stmt = stmt.join(Project).where(Project.name == project)
    meta = {}
    for s in d.scalars(stmt):
        for st in s.stats:
            meta.setdefault(st.metric, metric_meta(st.metric, s.metric_meta))
    return {"metrics": ordered(list(meta), meta), "metric_meta": meta}


@app.get("/api/plugins")
def server_plugins():
    return hooks.describe()

"""Uploads a finished local session to the server as one gzipped JSON bundle.

The bundle carries display info for every metric it contains (metric_meta), so the
server never needs to know in advance which metrics exist. New collector plugins
and business metrics show up on the server with correct names and units."""
import gzip
import io
import json
import time
import zipfile
from urllib.parse import quote

import requests

from . import plugin

BUNDLE_FORMAT = 2
SHOT_BATCH = 300   # screenshots per request (~10 MB)


def build_bundle(store, sid) -> dict:
    plugin.load_plugins()
    s = store.get_session(sid)
    if not s:
        raise ValueError("本地找不到这个会话")
    if s["status"] == "running":
        raise ValueError("会话还在进行中，结束后再上传")
    points = store.points(sid)
    cfg = s.get("config") or {}
    platform = cfg.get("platform") or (s.get("device") or {}).get("platform") or "android"
    overrides = cfg.get("metric_meta") or {}
    meta = {m: plugin.metric_meta(m, platform, overrides) for m in sorted({p[1] for p in points})}
    return {
        "format": BUNDLE_FORMAT,
        "session": {k: s[k] for k in ("id", "name", "project", "tags", "notes", "device",
                                      "app_package", "app_version", "config",
                                      "started_at", "ended_at", "status")},
        "metric_meta": meta,
        "points": points,
        "events": store.events(sid),
        "attachments": store.attachments(sid),   # crash logs
    }


def upload(store, sid, server_url, token=None, user=None, retries=3):
    if not server_url:
        raise ValueError("还没有设置服务器地址")
    body = gzip.compress(json.dumps(build_bundle(store, sid), ensure_ascii=False).encode())
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if user:
        headers["X-User"] = quote(user)  # headers must be ASCII; server unquotes
    base = server_url.rstrip("/")
    remote_id = _post(base + "/api/sessions/upload", ("session.json.gz", body, "application/gzip"),
                      headers, retries)["id"]
    shots = store.shots(sid)
    for i in range(0, len(shots), SHOT_BATCH):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # JPEGs don't compress further
            for ts in shots[i:i + SHOT_BATCH]:
                z.write(store.shot_path(sid, ts), f"{ts}.jpg")
        _post(f"{base}/api/sessions/{quote(remote_id)}/screenshots?reset={int(i == 0)}",
              ("shots.zip", buf.getvalue(), "application/zip"), headers, retries)
    store.mark_uploaded(sid, remote_id, int(time.time() * 1000))
    return remote_id


def _post(url, file, headers, retries):
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, files={"file": file}, headers=headers, timeout=300)
            if r.status_code >= 400:
                raise RuntimeError(f"服务器返回 {r.status_code}: {r.text[:200]}")
            return r.json()
        except (requests.RequestException, RuntimeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"上传失败: {last}")

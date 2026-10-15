"""Pluggable auth. Replace get_user() with the company SSO / tool platform later;
nothing else in the server reads identity directly.

AUTH_MODE=none   (default) user comes from X-User header, or "anonymous"
AUTH_MODE=token  AUTH_TOKENS="tokenA:alice,tokenB:bob", sent as "Authorization: Bearer tokenA"
"""
import os
from urllib.parse import unquote

from fastapi import HTTPException, Request

MODE = os.environ.get("AUTH_MODE", "none")
TOKENS = dict(p.split(":", 1) for p in os.environ.get("AUTH_TOKENS", "").split(",") if ":" in p)


def get_user(request: Request) -> str:
    if MODE == "token":
        h = request.headers.get("authorization", "")
        tok = h[7:] if h.lower().startswith("bearer ") else ""
        if tok not in TOKENS:
            raise HTTPException(401, "未登录或 token 无效")
        return TOKENS[tok]
    return unquote(request.headers.get("x-user") or "") or "anonymous"


def can_modify(user: str, owner: str) -> bool:
    return MODE == "none" or user == owner

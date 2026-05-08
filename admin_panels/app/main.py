"""Admin Panels launcher addon: tile launcher + reverse proxy via HA ingress."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("admin_panels")

OPTIONS_PATH = Path(os.environ.get("ADMIN_PANELS_OPTIONS", "/data/options.json"))
APP_DIR = Path(__file__).resolve().parent

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

DEFAULT_PING_INTERVAL = 30
DEFAULT_REQUEST_TIMEOUT = 5


def _load_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        log.warning("Options file %s not found, using empty config", OPTIONS_PATH)
        return {"panels": [], "ping_interval": DEFAULT_PING_INTERVAL, "request_timeout": DEFAULT_REQUEST_TIMEOUT}
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.error("Failed to read options: %s", e)
        return {"panels": [], "ping_interval": DEFAULT_PING_INTERVAL, "request_timeout": DEFAULT_REQUEST_TIMEOUT}


OPTIONS = _load_options()
PANELS: list[dict[str, Any]] = OPTIONS.get("panels") or []
PING_INTERVAL = int(OPTIONS.get("ping_interval") or DEFAULT_PING_INTERVAL)
REQUEST_TIMEOUT = float(OPTIONS.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT)

log.info("Loaded %d panel(s), ping_interval=%ds, timeout=%ss", len(PANELS), PING_INTERVAL, REQUEST_TIMEOUT)

app = FastAPI(title="Admin Panels", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _normalize_panel(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": p.get("name", "Untitled"),
        "url": (p.get("url") or "").rstrip("/"),
        "icon": p.get("icon") or "mdi:web",
        "verify_tls": bool(p.get("verify_tls", True)),
    }


NORMALIZED = [_normalize_panel(p) for p in PANELS]


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def _ingress_prefix(request: Request) -> str:
    """Return the absolute ingress path prefix HA assigns to this addon, or ''."""
    return request.headers.get("x-ingress-path", "").rstrip("/")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "panels": NORMALIZED,
            "ping_interval": PING_INTERVAL,
            "ingress_path": _ingress_prefix(request),
        },
    )


@app.get("/api/ping")
async def ping_all() -> JSONResponse:
    async def check(idx: int, panel: dict[str, Any]) -> dict[str, Any]:
        url = panel["url"]
        if not url:
            return {"idx": idx, "ok": False, "status": None, "error": "no url"}
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                verify=panel["verify_tls"],
                follow_redirects=False,
            ) as client:
                r = await client.get(url)
            return {"idx": idx, "ok": r.status_code < 500, "status": r.status_code, "error": None}
        except httpx.TimeoutException:
            return {"idx": idx, "ok": False, "status": None, "error": "timeout"}
        except Exception as e:
            return {"idx": idx, "ok": False, "status": None, "error": type(e).__name__}

    results = await asyncio.gather(*(check(i, p) for i, p in enumerate(NORMALIZED)))
    return JSONResponse(list(results))


# ---------------------------------------------------------------------------
# Reverse proxy
# ---------------------------------------------------------------------------


_INJECTED_SCRIPT_TEMPLATE = """
<script>
(function() {
    var IDX = "__IDX__";
    var path = window.location.pathname;
    var marker = "/proxy/" + IDX + "/";
    var cut = path.indexOf(marker);
    var BASE = cut >= 0 ? path.substring(0, cut + marker.length) : "/";

    function rewrite(url) {
        if (typeof url !== "string") return url;
        if (url.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(url)) return url;
        if (url.startsWith("/")) {
            var trimmed = BASE.endsWith("/") ? BASE.slice(0, -1) : BASE;
            return trimmed + url;
        }
        return url;
    }

    var origFetch = window.fetch;
    if (origFetch) {
        window.fetch = function(input, init) {
            if (typeof input === "string") {
                input = rewrite(input);
            } else if (input && input.url) {
                try { input = new Request(rewrite(input.url), input); } catch (e) {}
            }
            return origFetch.call(this, input, init);
        };
    }

    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        var args = Array.prototype.slice.call(arguments);
        args[1] = rewrite(url);
        return origOpen.apply(this, args);
    };
})();
</script>
"""


def _attr_rewriter(base_path: str) -> re.Pattern[bytes]:
    return re.compile(rb'(\b(?:href|src|action|formaction|data-src)\s*=\s*)(["\'])(/(?!/)[^"\']*)\2')


_ATTR_RE = re.compile(rb'(\b(?:href|src|action|formaction|data-src)\s*=\s*)(["\'])(/(?!/)[^"\']*)\2')
_CSS_URL_RE = re.compile(rb'url\((["\']?)(/(?!/)[^)"\']*)\1\)')


def _rewrite_html(body: bytes, base_path: str, idx: int, final_base_href: str = "") -> bytes:
    base_b = base_path.rstrip("/").encode("utf-8")

    def attr_sub(m: re.Match[bytes]) -> bytes:
        return m.group(1) + m.group(2) + base_b + m.group(3) + m.group(2)

    body = _ATTR_RE.sub(attr_sub, body)
    body = _CSS_URL_RE.sub(lambda m: b"url(" + m.group(1) + base_b + m.group(2) + m.group(1) + b")", body)

    head_inject = b""
    if final_base_href:
        head_inject += b'<base href="' + final_base_href.encode("utf-8") + b'">'
    head_inject += _INJECTED_SCRIPT_TEMPLATE.replace("__IDX__", str(idx)).encode("utf-8")

    if b"<head" in body:
        body = re.sub(rb"(<head\b[^>]*>)", rb"\1" + head_inject, body, count=1, flags=re.IGNORECASE)
    else:
        body = head_inject + body
    return body


def _rewrite_css(body: bytes, base_path: str) -> bytes:
    base_b = base_path.rstrip("/").encode("utf-8")
    return _CSS_URL_RE.sub(lambda m: b"url(" + m.group(1) + base_b + m.group(2) + m.group(1) + b")", body)


@app.api_route(
    "/proxy/{idx}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy(idx: int, path: str, request: Request) -> Response:
    if idx < 0 or idx >= len(NORMALIZED):
        raise HTTPException(status_code=404, detail="Unknown panel")
    panel = NORMALIZED[idx]
    if not panel["url"]:
        raise HTTPException(status_code=502, detail="Panel has no URL configured")

    target = panel["url"] + "/" + path
    if request.url.query:
        target += "?" + request.url.query

    ingress = _ingress_prefix(request)
    base_path = f"{ingress}/proxy/{idx}/"

    upstream_headers = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in HOP_BY_HOP or kl in {"host", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for"}:
            continue
        upstream_headers[k] = v
    upstream_headers["accept-encoding"] = "identity"

    body = await request.body()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=REQUEST_TIMEOUT),
            verify=panel["verify_tls"],
            follow_redirects=True,
            max_redirects=10,
        ) as client:
            upstream = await client.request(
                request.method,
                target,
                content=body if body else None,
                headers=upstream_headers,
            )
    except httpx.TooManyRedirects:
        raise HTTPException(status_code=502, detail="Upstream redirect loop") from None
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout") from None
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"Upstream connect error: {e}") from None
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from None

    content_type = upstream.headers.get("content-type", "")
    body_out = upstream.content

    final_path = upstream.url.path or "/"
    final_dir = posixpath.dirname(final_path)
    if not final_dir.endswith("/"):
        final_dir += "/"
    final_base_href = base_path.rstrip("/") + final_dir

    if "text/html" in content_type:
        body_out = _rewrite_html(body_out, base_path, idx, final_base_href)
    elif "text/css" in content_type:
        body_out = _rewrite_css(body_out, base_path)

    response_headers = {}
    for k, v in upstream.headers.items():
        kl = k.lower()
        if kl in HOP_BY_HOP:
            continue
        if kl == "location":
            v = _rewrite_redirect(v, panel["url"], base_path)
        response_headers[k] = v

    return Response(
        content=body_out,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type or None,
    )


def _rewrite_redirect(location: str, upstream_base: str, proxy_base: str) -> str:
    if not location:
        return location
    parsed = urlparse(location)
    if parsed.scheme and parsed.netloc:
        upstream_parsed = urlparse(upstream_base)
        if parsed.netloc == upstream_parsed.netloc:
            return proxy_base.rstrip("/") + parsed.path + (("?" + parsed.query) if parsed.query else "")
        return location
    if location.startswith("/"):
        return proxy_base.rstrip("/") + location
    return location

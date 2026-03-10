"""
MSI Auth Proxy
==============
An async reverse proxy that sits between DIAL Core and Azure AI Foundry.

Authentication: AKS Workload Identity (Microsoft Entra Workload ID)
--------------------------------------------------------------------
The AKS mutating admission webhook injects three environment variables and a
projected service account token volume into every pod that carries the label
  azure.workload.identity/use: "true"

  AZURE_CLIENT_ID             — client ID of the managed identity
  AZURE_TENANT_ID             — Azure AD tenant ID
  AZURE_FEDERATED_TOKEN_FILE  — path to the projected Kubernetes SA token

DefaultAzureCredential reads those variables automatically and exchanges the
Kubernetes token for a Microsoft Entra access token via OIDC federation.
Token caching and refresh are handled entirely by the SDK — no custom cache
is needed.

For every incoming request the proxy:
  1. Strips the DIAL-internal "api-key" header.
  2. Calls DefaultAzureCredential.get_token() — returns from SDK cache on
     the hot path, transparently refreshes when the token nears expiry.
  3. Injects  Authorization: Bearer <token>  into the forwarded request.
  4. Streams the upstream response back to the caller unchanged.

Configuration (environment variables)
--------------------------------------
TARGET_URL      Required.  Azure AI Foundry base URL.
                e.g.  https://<resource>.openai.azure.com
TOKEN_SCOPE     Optional.  OAuth2 scope for the token request.
                Default: https://cognitiveservices.azure.com/.default
LISTEN_HOST     Optional.  Default: 0.0.0.0
LISTEN_PORT     Optional.  Default: 8080
LOG_LEVEL       Optional.  debug | info | warning | error.  Default: info

The AZURE_* variables are injected automatically by the Workload Identity
webhook — do not set them manually.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return value


TARGET_URL  = _require_env("TARGET_URL").rstrip("/")
TOKEN_SCOPE = os.environ.get(
    "TOKEN_SCOPE", "https://cognitiveservices.azure.com/.default"
).strip()

# Headers that must never be forwarded in either direction (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "proxy-authenticate",
        "proxy-authorization",
        "upgrade",
        "content-length",  # httpx recalculates this
    }
)

# ── Shared resources (initialised in lifespan) ────────────────────────────────

_credential: Optional[DefaultAzureCredential] = None
_http_client: Optional[httpx.AsyncClient] = None


def credential() -> DefaultAzureCredential:
    if _credential is None:
        raise RuntimeError("Credential is not initialised")
    return _credential


def http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client is not initialised")
    return _http_client


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _credential, _http_client

    # Validate that the Workload Identity webhook injected the expected vars.
    for var in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"):
        if not os.environ.get(var):
            logger.warning(
                "Expected Workload Identity env var '%s' is not set. "
                "Make sure the pod has label azure.workload.identity/use=true "
                "and the ServiceAccount has the client-id annotation.",
                var,
            )

    logger.info(
        "Starting MSI auth proxy  target=%s  scope=%s  client_id=%s",
        TARGET_URL,
        TOKEN_SCOPE,
        os.environ.get("AZURE_CLIENT_ID", "<not set>"),
    )

    _credential = DefaultAzureCredential()

    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        follow_redirects=True,
    )

    # Pre-warm: exchange the Kubernetes SA token for an Entra token now so the
    # first real request is not slowed down by an OIDC round-trip.
    try:
        token = await _credential.get_token(TOKEN_SCOPE)
        logger.info(
            "Entra token pre-warmed  expires_on=%s",
            token.expires_on,
        )
    except Exception as exc:
        logger.warning("Token pre-warm failed (will retry per-request): %s", exc)

    yield  # Application runs here.

    logger.info("Shutting down — closing credential and HTTP client")
    await _credential.close()
    await _http_client.aclose()


app = FastAPI(
    title="MSI Auth Proxy",
    description="Injects Azure Managed Identity tokens for DIAL → Azure AI Foundry requests",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# ── Health endpoints ──────────────────────────────────────────────────────────

@app.get("/healthz", tags=["ops"])
async def liveness() -> dict:
    """Always returns 200. Used by the Kubernetes liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readiness():
    """Returns 200 only when a valid Entra token can be obtained.
    Returns 503 if the Workload Identity exchange fails, which removes the
    pod from Service endpoints until authentication recovers.
    """
    try:
        await credential().get_token(TOKEN_SCOPE)
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "error": str(exc)},
        )


# ── Proxy ─────────────────────────────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy(request: Request, path: str):
    """Forward every request to TARGET_URL with an injected Entra bearer token."""

    # 1. Acquire token — returns from the SDK's in-memory cache on the hot
    #    path; transparently refreshes ~2 minutes before expiry.
    try:
        token_obj = await credential().get_token(TOKEN_SCOPE)
    except Exception as exc:
        logger.error("Cannot acquire Entra token: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"Auth proxy could not obtain token: {exc}",
                    "type": "proxy_error",
                }
            },
        )

    # 2. Build the upstream URL, preserving path and query string exactly.
    upstream_url = f"{TARGET_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # 3. Filter request headers:
    #    - Drop hop-by-hop headers.
    #    - Drop "host"          — httpx sets the correct upstream Host.
    #    - Drop "api-key"       — DIAL-internal; Azure does not understand it.
    #    - Drop "authorization" — replaced by the Entra token below.
    filtered_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in ("host", "api-key", "authorization")
    }
    filtered_headers["authorization"] = f"Bearer {token_obj.token}"

    # 4. Read the full request body (typical LLM request ≪ 1 MB).
    body = await request.body()

    logger.debug(
        "→ %s %s  upstream=%s  body_bytes=%d",
        request.method,
        request.url.path,
        upstream_url,
        len(body),
    )

    # 5. Send to upstream with streaming enabled so SSE / chunked transfer
    #    responses flow back without buffering.
    try:
        upstream_req = http_client().build_request(
            method=request.method,
            url=upstream_url,
            headers=filtered_headers,
            content=body,
        )
        upstream_resp = await http_client().send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        logger.error("Upstream request failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"Upstream unreachable: {exc}",
                    "type": "proxy_error",
                }
            },
        )

    logger.debug(
        "← %d  content-type=%s",
        upstream_resp.status_code,
        upstream_resp.headers.get("content-type", ""),
    )

    # 6. Filter response headers before forwarding back to the caller.
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    # 7. Stream response body. BackgroundTask closes the upstream connection
    #    only after the last byte has been sent to the caller.
    return StreamingResponse(
        content=upstream_resp.aiter_bytes(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


# ── Entry-point (local development) ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("LISTEN_HOST", "0.0.0.0"),
        port=int(os.environ.get("LISTEN_PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )

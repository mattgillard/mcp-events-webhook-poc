"""Day-one MCP Events webhook experiment. No Fastmail dependency.

This intentionally implements the small MCP 2026-07-28 JSON-RPC subset directly:
server/discover, events/list, events/subscribe, events/unsubscribe.
It is NOT a general-purpose MCP server or production-safe webhook proxy.
"""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="MCP Events Day One")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mcp.debug")
PROTOCOL = "2026-07-28"
EVENT_NAME = "email.received"
TEST_TOKEN = os.getenv("TEST_TOKEN", "")
SUBSCRIPTIONS: dict[str, dict] = {}

EVENT = {
    "name": EVENT_NAME,
    "description": "A test email arrived; ask ChatGPT to summarise this synthetic email.",
    "delivery": ["webhook"],
    "inputSchema": {
        "type": "object", "properties": {
            "from": {"type": "string"},
            "subject_contains": {"type": "string"},
        }, "additionalProperties": False,
    },
    "payloadSchema": {
        "type": "object", "properties": {
            "email_id": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "received_at": {"type": "string"},
        },
        "required": ["email_id", "from", "subject", "received_at"],
        "additionalProperties": False,
    },
}


TOOL = {
    "name": "subscription_status",
    "description": "Report how many email.received test subscriptions this POC server holds.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True},
}


def rpc_result(req_id, value):
    return {"jsonrpc": "2.0", "id": req_id, "result": value}


def rpc_error(req_id, code, message, data=None):
    body = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": body}


async def check_callback_url(url: str):
    """Basic gate only. Production needs DNS-pinned transport and redirects off."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Callback must be an HTTPS URL without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("Callback must use port 443")
    # Reject known local/private DNS resolutions (not sufficient against DNS rebinding).
    try:
        addrs = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Callback host could not be resolved") from exc
    if not addrs or any(not ipaddress.ip_address(item[4][0]).is_global for item in addrs):
        raise ValueError("Callback resolves to a non-public address")


def validate_secret(secret: str):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("Missing whsec_ signing secret")
    try:
        raw = base64.b64decode(secret[6:], validate=True)
    except Exception as exc:
        raise ValueError("Invalid base64 secret") from exc
    if not (24 <= len(raw) <= 64):
        raise ValueError("Signing key must decode to 24–64 bytes")
    return raw


def signed_headers(secret, subscription_id, message_id, body):
    key = validate_secret(secret)
    timestamp = str(int(time.time()))
    material = f"{message_id}.{timestamp}.{body}".encode("utf-8")
    digest = base64.b64encode(hmac.new(key, material, hashlib.sha256).digest()).decode()
    return {
        "Content-Type": "application/json",
        "webhook-id": message_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{digest}",
        "X-MCP-Subscription-Id": subscription_id,
    }


async def post_signed(sub, message_id, payload):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode()) > 262144:
        raise ValueError("Event too large")
    await check_callback_url(sub["url"])
    headers = signed_headers(sub["secret"], sub["id"], message_id, body)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        return await client.post(sub["url"], content=body.encode(), headers=headers)


def parse_filters(arguments):
    if not isinstance(arguments, dict) or (set(arguments) - {"from", "subject_contains"}):
        raise ValueError("Unknown subscription arguments")
    if any(not isinstance(v, str) for v in arguments.values()):
        raise ValueError("Filter values must be strings")
    return arguments


@app.middleware("http")
async def log_mcp_requests(request: Request, call_next):
    if request.url.path == "/mcp" and request.method == "POST":
        body = await request.body()

        try:
            payload = json.loads(body)
            logger.info(
                "MCP request: method=%s id=%s",
                payload.get("method"),
                payload.get("id"),
            )
        except (ValueError, AttributeError):
            logger.warning(
                "Invalid MCP JSON-RPC request: content-type=%s len=%d body=%r",
                request.headers.get("content-type"),
                len(body),
                body[:500],
            )

    response = await call_next(request)

    logger.info(
        "HTTP %s %s -> %s",
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


@app.get("/health")
async def health():
    return {"ok": True, "subscriptions": len(SUBSCRIPTIONS)}


@app.post("/mcp")
async def mcp(request: Request):
    req = req_id = method = None
    try:
        try:
            req = await request.json()
        except ValueError:
            return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return JSONResponse(rpc_error(None, -32600, "Invalid Request"), status_code=400)
        if "id" not in req:
            # JSON-RPC notification (e.g. notifications/initialized): no response body.
            return Response(status_code=202)
        method = req["method"]
        params = req.get("params", {})
        req_id = req["id"]
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method == "server/discover":
            result = {
                "resultType": "complete", "supportedVersions": [PROTOCOL],
                "capabilities": {"tools": {}, "events": {}},
                "serverInfo": {"name": "mcp-events-day-one", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            if params.get("name") != TOOL["name"]:
                raise ValueError("Unknown tool")
            text = f"{len(SUBSCRIPTIONS)} active {EVENT_NAME} subscription(s)."
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        elif method == "events/list":
            result = {"events": [EVENT]}
        elif method == "events/subscribe":
            if params.get("name") != EVENT_NAME:
                raise ValueError("Unknown event")
            filters = parse_filters(params.get("arguments", {}))
            delivery = params.get("delivery", {})
            if delivery.get("mode") != "webhook":
                raise ValueError("Only webhook delivery supported")
            url, secret = delivery.get("url"), delivery.get("secret")
            if not isinstance(url, str):
                raise ValueError("Missing callback URL")
            await check_callback_url(url)
            validate_secret(secret)
            identity = json.dumps([url, EVENT_NAME, filters], sort_keys=True)
            sub_id = "sub_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            sub = {"id": sub_id, "url": url, "secret": secret, "filters": filters}
            challenge = secrets.token_urlsafe(32)
            verification_id = "msg_" + secrets.token_hex(12)
            verification = {"type": "verification", "challenge": challenge}
            try:
                response = await post_signed(sub, verification_id, verification)
                response.raise_for_status()
                echoed = response.json().get("challenge", "")
                if not isinstance(echoed, str) or not hmac.compare_digest(challenge, echoed):
                    raise ValueError("Challenge was not echoed")
            except Exception as exc:
                print(f"callback verification failed: {type(exc).__name__}: {exc}", flush=True)
                return JSONResponse(rpc_error(req_id, -32015, "CallbackEndpointError", {"reason": "challenge_failed"}))
            SUBSCRIPTIONS[sub_id] = sub
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
            result = {"id": sub_id, "refreshBefore": expires, "cursor": None, "truncated": False}
            print(f"subscription verified: {sub_id}", flush=True)
        elif method == "events/unsubscribe":
            filters = parse_filters(params.get("arguments", {}))
            delivery = params.get("delivery", {})
            identity = json.dumps([delivery.get("url"), params.get("name"), filters], sort_keys=True)
            sub_id = "sub_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            SUBSCRIPTIONS.pop(sub_id, None)
            result = {}
        else:
            return JSONResponse(rpc_error(req_id, -32601, "Method not found"))
        return JSONResponse(rpc_result(req_id, result))
    except ValueError as exc:
        # Validation failures raise ValueError with client-safe messages.
        return JSONResponse(rpc_error(req_id, -32602, str(exc)))
    except Exception:
        logger.exception("Unhandled error in %s", method)
        return JSONResponse(rpc_error(req_id, -32603, "Internal error"))


class TestEmail(BaseModel):
    email_id: str = "test-001"
    from_address: str = "poc@example.com"
    subject: str = "MCP event test"


@app.post("/test")
async def test_event(email: TestEmail, x_test_token: str | None = Header(None)):
    if not TEST_TOKEN or not x_test_token or not hmac.compare_digest(TEST_TOKEN, x_test_token):
        raise HTTPException(401, "Invalid X-Test-Token")
    data = {
        "email_id": email.email_id,
        "from": email.from_address,
        "subject": email.subject,
        "received_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    out = []
    for sub in list(SUBSCRIPTIONS.values()):
        filt = sub["filters"]
        if filt.get("from") and filt["from"].lower() != data["from"].lower():
            continue
        if filt.get("subject_contains") and filt["subject_contains"].lower() not in data["subject"].lower():
            continue
        event_id = "evt_" + secrets.token_hex(12)
        event = {"eventId": event_id, "name": EVENT_NAME, "timestamp": data["received_at"], "data": data, "cursor": None}
        try:
            response = await post_signed(sub, event_id, event)
            result = {"subscription": sub["id"], "http_status": response.status_code, "accepted": 200 <= response.status_code < 300}
            if response.status_code == 410:
                # Client has dropped this subscription: stop delivering to it.
                SUBSCRIPTIONS.pop(sub["id"], None)
                result["removed"] = True
                logger.info("subscription removed after 410: %s", sub["id"])
            elif response.status_code == 413:
                logger.warning("event rejected as too large (413), not retrying: %s", sub["id"])
            out.append(result)
        except Exception as exc:
            out.append({"subscription": sub["id"], "error": str(exc)})
    return {"matched": len(out), "deliveries": out}

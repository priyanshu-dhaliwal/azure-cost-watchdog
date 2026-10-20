"""
keepalive — HTTP trigger.

    POST /api/keepalive?minutes=120   {"resourceId": "/subscriptions/..."}

Self-service: a developer who needs a machine past its schedule says so, and gets
a grace period instead of a stopped VM. This endpoint exists because a cost system
people must work around is a cost system people disable.

It refuses (403) to extend anything that is not a shutdown candidate — a
keep-alive on production would be a way to grant an exception to the environment
gate, which must not be possible from an HTTP call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.armclient import ArmClient
from shared.store import ActionStore

log = logging.getLogger("cost.keepalive")

MAX_MINUTES = 8 * 60


async def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    resource_id = body.get("resourceId") or req.params.get("resourceId")
    if not resource_id:
        return _json({"error": "resourceId is required"}, 400)

    try:
        minutes = int(req.params.get("minutes", body.get("minutes", 60)))
    except (TypeError, ValueError):
        return _json({"error": "minutes must be an integer"}, 400)

    if minutes <= 0 or minutes > MAX_MINUTES:
        return _json({"error": f"minutes must be between 1 and {MAX_MINUTES}"}, 400)

    requested_by = (req.headers.get("x-ms-client-principal-name")
                    or body.get("requestedBy") or "unknown")

    credential = DefaultAzureCredential()
    arm = ArmClient(credential)
    store = ActionStore(credential)

    try:
        live = await arm.get(resource_id)
        env = _tag(live["tags"], "environment").lower()

        if env in ("prod", "production", "prd", "live"):
            return _json({"error": "production resources are not managed by the watchdog"}, 403)
        if _tag(live["tags"], "autoShutdown").lower() not in ("true", "yes", "1"):
            return _json({"error": "resource is not an auto-shutdown candidate"}, 403)

        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await arm.tag(resource_id, {"keepAliveUntil": until.strftime("%Y-%m-%dT%H:%M:%SZ")})
        await store.set_keepalive(resource_id, env or "unknown", until, requested_by)

        log.info("keep-alive on %s for %d min by %s", live["name"], minutes, requested_by)
        return _json({
            "resourceId": resource_id,
            "resourceName": live["name"],
            "keepAliveUntil": until.isoformat(),
            "minutes": minutes,
            "requestedBy": requested_by,
        }, 200)
    except Exception as exc:  # noqa: BLE001
        log.exception("keep-alive failed")
        return _json({"error": "could not set keep-alive", "detail": str(exc)}, 500)
    finally:
        await arm.close()
        await store.close()
        await credential.close()


def _tag(tags: dict[str, str], key: str) -> str:
    for k, v in (tags or {}).items():
        if k.lower() == key.lower():
            return (v or "").strip()
    return ""


def _json(payload: dict, status: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")

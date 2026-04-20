from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from supabase import create_client
import os

app = FastAPI()

VERIFY_TOKEN = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
DELETE_DATA_ON_DEAUTHORIZE = os.getenv("DELETE_DATA_ON_DEAUTHORIZE", "false").lower() == "true"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


@app.get("/")
def healthcheck():
    return {"status": "ok", "service": "pacesense-strava-webhook"}


@app.get("/strava/webhook")
def validate_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="Invalid hub.mode")

    if not VERIFY_TOKEN or hub_verify_token != VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid verify token")

    return {"hub.challenge": hub_challenge}


@app.post("/strava/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()
    supabase = get_supabase()

    event_row = {
        "subscription_id": payload.get("subscription_id"),
        "owner_id": payload.get("owner_id"),
        "object_id": payload.get("object_id"),
        "object_type": payload.get("object_type"),
        "aspect_type": payload.get("aspect_type"),
        "updates": payload.get("updates", {}) or {},
        "event_time": payload.get("event_time"),
        "processing_status": "pending",
    }

    inserted = supabase.table("strava_webhook_events").insert(event_row).execute()
    inserted_row = inserted.data[0] if inserted.data else None

    try:
        object_type = payload.get("object_type")
        aspect_type = payload.get("aspect_type")
        owner_id = payload.get("owner_id")
        updates = payload.get("updates", {}) or {}

        if owner_id:
            supabase.table("users").update(
                {
                    "last_webhook_event_at": now_iso(),
                    "updated_at": now_iso(),
                }
            ).eq("strava_athlete_id", str(owner_id)).execute()

        if (
            object_type == "athlete"
            and aspect_type == "update"
            and str(updates.get("authorized")).lower() == "false"
        ):
            user_res = (
                supabase.table("users")
                .select("id")
                .eq("strava_athlete_id", str(owner_id))
                .limit(1)
                .execute()
            )

            user_id = user_res.data[0]["id"] if user_res.data else None

            supabase.table("users").update(
                {
                    "is_authorized": False,
                    "deauthorized_at": now_iso(),
                    "access_token": None,
                    "refresh_token": None,
                    "expires_at": None,
                    "updated_at": now_iso(),
                }
            ).eq("strava_athlete_id", str(owner_id)).execute()

            if DELETE_DATA_ON_DEAUTHORIZE and user_id:
                supabase.table("activities_raw").delete().eq("user_id", user_id).execute()
                supabase.table("metrics_daily").delete().eq("user_id", user_id).execute()

        if inserted_row:
            supabase.table("strava_webhook_events").update(
                {
                    "processing_status": "processed",
                    "processed_at": now_iso(),
                    "processing_error": None,
                }
            ).eq("id", inserted_row["id"]).execute()

    except Exception as exc:
        if inserted_row:
            supabase.table("strava_webhook_events").update(
                {
                    "processing_status": "failed",
                    "processed_at": now_iso(),
                    "processing_error": str(exc)[:2000],
                }
            ).eq("id", inserted_row["id"]).execute()

    return JSONResponse({"received": True}, status_code=200)
from datetime import datetime, timezone
import os

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from supabase import create_client

app = FastAPI()

VERIFY_TOKEN = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
DELETE_DATA_ON_DEAUTHORIZE = os.getenv("DELETE_DATA_ON_DEAUTHORIZE", "false").lower() == "true"

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITY_DETAIL_URL = "https://www.strava.com/api/v3/activities"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def refresh_access_token(refresh_token: str) -> dict:
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        raise RuntimeError("Missing STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET")

    response = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    token_data = response.json()

    if "access_token" not in token_data or "refresh_token" not in token_data:
        raise RuntimeError(f"Invalid refresh response from Strava: {token_data}")

    return token_data


def fetch_activity_by_id(access_token: str, activity_id: int | str) -> dict:
    response = requests.get(
        f"{STRAVA_ACTIVITY_DETAIL_URL}/{int(activity_id)}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_user_by_strava_athlete_id(supabase, strava_athlete_id: str):
    res = (
        supabase.table("users")
        .select("*")
        .eq("strava_athlete_id", str(strava_athlete_id))
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def ensure_valid_access_token(supabase, user: dict) -> str:
    refresh_token = user.get("refresh_token")
    access_token = user.get("access_token")

    if not refresh_token:
        raise RuntimeError("User has no refresh token.")

    token_data = refresh_access_token(refresh_token)

    supabase.table("users").update(
        {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": datetime.fromtimestamp(
                token_data["expires_at"], tz=timezone.utc
            ).isoformat(),
            "is_authorized": True,
            "deauthorized_at": None,
            "updated_at": now_iso(),
        }
    ).eq("id", user["id"]).execute()

    return token_data.get("access_token", access_token)


def upsert_activity_row(supabase, user_id: str, activity: dict) -> None:
    row = {
        "user_id": user_id,
        "activity_id": activity.get("id"),
        "start_date": activity.get("start_date"),
        "sport_type": activity.get("type") or activity.get("sport_type"),
        "raw_json": activity,
        "updated_at": now_iso(),
    }
    supabase.table("activities_raw").upsert(row, on_conflict="activity_id").execute()


def delete_activity_row(supabase, activity_id: int | str) -> None:
    supabase.table("activities_raw").delete().eq("activity_id", int(activity_id)).execute()


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
        object_id = payload.get("object_id")
        updates = payload.get("updates", {}) or {}

        if owner_id:
            supabase.table("users").update(
                {
                    "last_webhook_event_at": now_iso(),
                    "updated_at": now_iso(),
                }
            ).eq("strava_athlete_id", str(owner_id)).execute()

        # Athlete deauthorization
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

        # Activity create/update/delete
        elif object_type == "activity" and owner_id and object_id:
            user = get_user_by_strava_athlete_id(supabase, str(owner_id))

            if user and user.get("is_authorized", True):
                if aspect_type == "delete":
                    delete_activity_row(supabase, object_id)
                elif aspect_type in {"create", "update"}:
                    access_token = ensure_valid_access_token(supabase, user)
                    activity = fetch_activity_by_id(access_token, object_id)
                    upsert_activity_row(supabase, user["id"], activity)

                    supabase.table("users").update(
                        {
                            "last_activity_sync_at": now_iso(),
                            "updated_at": now_iso(),
                        }
                    ).eq("id", user["id"]).execute()

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
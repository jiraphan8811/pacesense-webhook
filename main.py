from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
import os

app = FastAPI()

VERIFY_TOKEN = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "")


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
    print("Strava webhook event:", payload)
    return JSONResponse({"received": True}, status_code=200)
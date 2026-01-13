import logging
import httpx
import json
import os
import urllib.parse
from datetime import datetime

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

#test

app = FastAPI()
logging.basicConfig(level=logging.INFO)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

def get_sheets_creds():
    creds_json = os.getenv("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDS_JSON")
    return Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

def twiml_play(url: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{url}</Play>
</Response>
"""

async def slack_notify(text: str):
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json={"text": text})

@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def voice(request: Request):
    base_url = str(request.base_url).rstrip("/")

    text = "Hello. This is the property manager assistant. Press 1 for maintenance. Press 2 for leasing."
    audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather numDigits="1" action="{base_url}/twilio/route" method="POST">
    <Play>{audio_url}</Play>
  </Gather>
  <Play>{base_url}/tts?{urllib.parse.urlencode({'text': 'We did not receive your selection. Goodbye.'})}</Play>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")


@app.api_route("/twilio/route", methods=["GET", "POST"])
async def route(request: Request):
    form = await request.form()
    # Force HTTPS to prevent redirects
    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
    
    logging.info(f"Route called. Method: {request.method}, URL: {request.url}")
    logging.info(f"Query params: {request.query_params}")
    form = await request.form()
    logging.info(f"Form data: {form}")
    
    val = form.get("Digits") or request.query_params.get("Digits")
    digit = str(val).strip() if val else None
    logging.info(f"Resolved digit: '{digit}' (from '{val}')")

    if digit == "1":
        base_url = str(request.base_url).rstrip("/")
        text = "You selected maintenance. After the beep, please describe the issue, your unit number, and the best callback number. Press pound when finished."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/maintenance_recorded"
        thanks_text = "Thank you. We received your maintenance request. Goodbye."
        thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': thanks_text})}"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Play>{audio_url}</Play>
    <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
    </Response>
    """
        return Response(content=twiml, media_type="application/xml")

    elif digit == "2":
        base_url = str(request.base_url).rstrip("/")
        text = "You selected leasing. After the beep, please leave your name, the property you're interested in, and the best callback number. Press pound when finished."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/leasing_recorded"
        thanks_text = "Thank you. We received your leasing inquiry. Goodbye."
        thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': thanks_text})}"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Play>{audio_url}</Play>
    <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
    </Response>
    """
        return Response(content=twiml, media_type="application/xml")

    else:
        text = "Invalid selection. Goodbye."

    base_url = str(request.base_url).rstrip("/")
    q = urllib.parse.urlencode({"text": text})
    audio_url = f"{base_url}/tts?{q}"

    return Response(content=twiml_play(audio_url), media_type="application/xml")

@app.get("/twilio_say")
async def twilio_say(text: str):
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">{text}</Say>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@app.get("/tts")
async def tts(text: str):
    logging.info("TTS requested: %s", text)

    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logging.error("Missing ElevenLabs config")
        return Response("Missing ElevenLabs config", status_code=500)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)

        if r.status_code != 200:
            logging.error("ElevenLabs error %s: %s", r.status_code, r.text)
            return Response("TTS unavailable", status_code=503)

        return StreamingResponse(iter([r.content]), media_type="audio/mpeg")

@app.api_route("/twilio/maintenance_recorded", methods=["GET", "POST"])
async def maintenance_recorded(request: Request):
    form = await request.form()

    logging.info(f"Maintenance callback hit. From: {from_number}, Url: {mp3_url}")

    try:
        append_maintenance_row([
            datetime.utcnow().isoformat(),
            from_number or "",
            mp3_url,
        ])
        logging.info("Sheets append successful")
    except Exception as e:
        logging.error(f"Sheets append failed: {e}")

    await slack_notify(f"🛠️ Maintenance voicemail from {from_number}\nRecording: {mp3_url}")
    
    base_url = str(request.base_url).rstrip("/").replace("/twilio/maintenance_recorded", "").replace("http://", "https://")
    thanks_text = "Thank you. We received your maintenance request. Goodbye."
    thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': thanks_text})}"

    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{thanks_url}</Play></Response>',
        media_type="application/xml",
    )

@app.api_route("/twilio/leasing_recorded", methods=["GET", "POST"])
async def leasing_recorded(request: Request):
    form = await request.form()

    logging.info(f"Leasing callback hit. From: {from_number}, Url: {mp3_url}")

    try:
        append_leasing_row([
            datetime.utcnow().isoformat(),
            from_number or "",
            mp3_url,
        ])
        logging.info("Sheets append successful")
    except Exception as e:
        logging.error(f"Sheets append failed: {e}")

    await slack_notify(f"🏠 Leasing voicemail from {from_number}\nRecording: {mp3_url}")

    base_url = str(request.base_url).rstrip("/").replace("/twilio/leasing_recorded", "").replace("http://", "https://")
    thanks_text = "Thank you. We received your leasing inquiry. Goodbye."
    thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': thanks_text})}"

    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{thanks_url}</Play></Response>',
        media_type="application/xml",
    )


def append_maintenance_row(values):
    creds = get_sheets_creds()
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Maintenance!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()

def append_leasing_row(values):
    creds = get_sheets_creds()
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Leasing!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()
    

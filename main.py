import os
import httpx
import json
import urllib.parse
from datetime import datetime

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse


app = FastAPI()

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "")

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
    digit = form.get("Digits")

    if digit == "1":
        # Prompt via ElevenLabs, then record the caller
        text = "You selected maintenance. After the beep, please describe the issue, your unit number, and the best callback number. Press pound when finished."
        base_url = str(request.base_url).rstrip("/")
        q = urllib.parse.urlencode({"text": text})
        audio_url = f"{base_url}/tts?{q}"
        record_action = f"{base_url}/twilio/maintenance_recorded"

        # Instead of only <Play>, do:
        # 1) Try <Play> ElevenLabs audio URL
        # 2) If it fails (audio fetch fails), Twilio continues and can <Say> as fallback

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
        <Gather numDigits="1" action="{base_url}/twilio/route" method="POST">
            <Play>{audio_url}</Play>
            <Say voice="alice">{text}</Say>
        </Gather>
        <Say voice="alice">We did not receive your selection. Goodbye.</Say>
        </Response>
        """

        return Response(content=twiml, media_type="application/xml")

    elif digit == "2":
        text = "You selected leasing. After the beep, please leave your name, the property you're interested in, and the best callback number. Press pound when finished."
        base_url = str(request.base_url).rstrip("/")

        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/leasing_recorded"
        thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': 'Thank you. We received your leasing inquiry. Goodbye.'})}"

        # Instead of only <Play>, do:
        # 1) Try <Play> ElevenLabs audio URL
        # 2) If it fails (audio fetch fails), Twilio continues and can <Say> as fallback

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
        <Gather numDigits="1" action="{base_url}/twilio/route" method="POST">
            <Play>{audio_url}</Play>
            <Say voice="alice">{text}</Say>
        </Gather>
        <Say voice="alice">We did not receive your selection. Goodbye.</Say>
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
    if not ELEVEN_KEY or not ELEVEN_VOICE:
        return Response("Missing ElevenLabs config", status_code=500)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
    headers = {
        "xi-api-key": ELEVEN_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)

        # If ElevenLabs blocks/fails, return 503 so caller flow can fall back
        if r.status_code != 200:
            return Response("TTS unavailable", status_code=503)

        return StreamingResponse(iter([r.content]), media_type="audio/mpeg")

@app.api_route("/twilio/maintenance_recorded", methods=["GET", "POST"])
async def maintenance_recorded(request: Request):
    form = await request.form()

    from_number = form.get("From")
    recording_url = form.get("RecordingUrl")
    mp3_url = f"{recording_url}.mp3" if recording_url else ""

    append_maintenance_row([
        datetime.utcnow().isoformat(),
        from_number or "",
        mp3_url,
    ])
    await slack_notify(f"🛠️ Maintenance voicemail from {from_number}\nRecording: {mp3_url}")

    return Response(
        content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
        media_type="application/xml",
    )

@app.api_route("/twilio/leasing_recorded", methods=["GET", "POST"])
async def leasing_recorded(request: Request):
    form = await request.form()

    from_number = form.get("From")
    recording_url = form.get("RecordingUrl")
    mp3_url = f"{recording_url}.mp3" if recording_url else ""

    append_leasing_row([
        datetime.utcnow().isoformat(),
        from_number or "",
        mp3_url,
    ])
    await slack_notify(f"🛠️ Maintenance voicemail from {from_number}\nRecording: {mp3_url}")

    return Response(
        content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
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
    

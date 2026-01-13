import os
import httpx
import urllib.parse
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse


app = FastAPI()

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "")

def twiml_play(url: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{url}</Play>
</Response>
"""

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

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
                <Response>
                <Play>{audio_url}</Play>
                <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
                <Play>{base_url}/tts?{urllib.parse.urlencode({"text":"Thank you. We received your maintenance request. Goodbye."})}</Play>
                </Response>
                """
        return Response(content=twiml, media_type="application/xml")

    elif digit == "2":
        text = "You selected leasing. After the beep, please leave your name, the property you're interested in, and the best callback number. Press pound when finished."
        base_url = str(request.base_url).rstrip("/")

        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/leasing_recorded"
        thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': 'Thank you. We received your leasing inquiry. Goodbye.'})}"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
                <Response>
                <Play>{audio_url}</Play>
                <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
                <Play>{thanks_url}</Play>
                </Response>
                """
        return Response(content=twiml, media_type="application/xml")

    else:
        text = "Invalid selection. Goodbye."

    base_url = str(request.base_url).rstrip("/")
    q = urllib.parse.urlencode({"text": text})
    audio_url = f"{base_url}/tts?{q}"

    return Response(content=twiml_play(audio_url), media_type="application/xml")


@app.get("/tts")
async def tts(text: str):
    # Twilio calls this endpoint via HTTP GET and expects audio bytes back.
    if not ELEVEN_KEY or not ELEVEN_VOICE:
        return Response("Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID", status_code=500)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
    headers = {
        "xi-api-key": ELEVEN_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            return Response(f"ElevenLabs error {r.status_code}: {r.text}", status_code=500)

        # Stream bytes back to Twilio
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

    return Response(
        content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
        media_type="application/xml",
    )


def append_maintenance_row(values):
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDS_PATH"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Maintenance!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()

def append_leasing_row(values):
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDS_PATH"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Leasing!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()
    

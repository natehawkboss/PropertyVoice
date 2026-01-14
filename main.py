import logging
import httpx
import json
import os
import urllib.parse
import time
from datetime import datetime

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from openai import AsyncOpenAI

app = FastAPI()
logging.basicConfig(level=logging.INFO)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
OPENAI_SECRET_KEY = os.getenv("OPENAI_SECRET_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

openai_client = AsyncOpenAI(api_key=OPENAI_SECRET_KEY) if OPENAI_SECRET_KEY else None

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

async def process_audio(recording_url: str):
    """Downloads audio and transcribes it using Whisper."""
    if not openai_client or not recording_url:
        return None

    try:
        logging.info(f"Downloading audio from {recording_url}")
        
        auth = None
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            
        async with httpx.AsyncClient() as client:
            response = await client.get(recording_url, auth=auth)
            if response.status_code != 200:
                logging.error(f"Failed to download audio: {response.status_code}")
                return None
            
            # OpenAI requires a filename
            file_obj = ("audio.mp3", response.content, "audio/mpeg")
            
            start = time.perf_counter()
            transcript = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=file_obj
            )
            duration = time.perf_counter() - start
            logging.info(f"[PERF] OpenAI Whisper transcription took {duration:.2f}s")
            
            logging.info(f"Transcription: {transcript.text}")
            return transcript.text
    except Exception as e:
        logging.error(f"Transcription failed: {e}")
        return None

async def classify_maintenance_issue(text: str):
    """Classifies the issue and returns specific advice."""
    if not openai_client or not text:
        return "Unknown", "Thank you. We have logged your maintenance request."

    system_prompt = """
    You are a property management assistant. Analyze the maintenance request.
    Output JSON: {"category": "Emergency" | "Urgent" | "Routine", "advice": "One sentence advice for the tenant.", "unit": "Unit number if mentioned, else Unknown", "property": "Property name if mentioned, else Unknown"}
    
    Rules:
    - If water leak/flood: Advise to turn off water valve immediately.
    - If fire/gas: Advise to call 911/emergency services and leave.
    - If no heat (winter): Advise to check thermostat batteries.
    - Otherwise: "We will prioritize this shortly."
    """
    
    try:
        start = time.perf_counter()
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            response_format={"type": "json_object"}
        )
        duration = time.perf_counter() - start
        logging.info(f"[PERF] OpenAI GPT-4o-mini classification took {duration:.2f}s")

        data = json.loads(response.choices[0].message.content)
        return (
            data.get("category", "Routine"),
            data.get("advice", "We have received your request."),
            data.get("unit", "Unknown"),
            data.get("property", "Unknown")
        )
    except Exception as e:
        logging.error(f"Classification failed: {e}")
        return "Error", "Thank you. We have logged your request."

@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def voice(request: Request):
    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
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
    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
    
    logging.info(f"Route called. Method: {request.method}, URL: {request.url}")
    form = await request.form()
    
    val = form.get("Digits") or request.query_params.get("Digits")
    digit = str(val).strip() if val else None
    logging.info(f"Resolved digit: '{digit}' (from '{val}')")

    if digit == "1":
        text = "You selected maintenance. After the beep, please describe the issue, your unit number, and the best callback number. Press pound when finished."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/maintenance_recorded"
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Play>{audio_url}</Play>
    <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
    </Response>
    """
        return Response(content=twiml, media_type="application/xml")

    elif digit == "2":
        text = "You selected leasing. After the beep, please leave your name, the property you're interested in, and the best callback number. Press pound when finished."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        record_action = f"{base_url}/twilio/leasing_recorded"
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Play>{audio_url}</Play>
    <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
    </Response>
    """
        return Response(content=twiml, media_type="application/xml")

    else:
        text = "Invalid selection. Goodbye."

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
        start = time.perf_counter()
        r = await client.post(url, headers=headers, json=payload)
        duration = time.perf_counter() - start
        logging.info(f"[PERF] ElevenLabs TTS generation took {duration:.2f}s")

        if r.status_code != 200:
            logging.error("ElevenLabs error %s: %s", r.status_code, r.text)
            return Response("TTS unavailable", status_code=503)

        return StreamingResponse(iter([r.content]), media_type="audio/mpeg")

@app.api_route("/twilio/maintenance_recorded", methods=["GET", "POST"])
async def maintenance_recorded(request: Request):
    form = await request.form()

    from_number = form.get("From") or request.query_params.get("From")
    recording_url = form.get("RecordingUrl") or request.query_params.get("RecordingUrl")
    mp3_url = f"{recording_url}.mp3" if recording_url else ""

    logging.info(f"Maintenance callback hit. From: {from_number}")
    
    # 1. Transcribe
    transcript = await process_audio(mp3_url)
    
    # 2. Classify
    category, advice, unit, property_name = await classify_maintenance_issue(transcript)
    logging.info(f"Issue: {category}, Advice: {advice}, Unit: {unit}, Property: {property_name}")

    try:
        append_maintenance_row([
            datetime.utcnow().isoformat(),
            from_number or "",
            mp3_url,
            transcript or "",
            category,
            advice,
            unit,
            property_name
        ])
        logging.info("Sheets append successful")
    except Exception as e:
        logging.error(f"Sheets append failed: {e}")

    await slack_notify(f"🛠️ Maintenance ({category}) from {from_number}\nTranscript: {transcript}\nAdvice Given: {advice}\nUnit: {unit}\nProperty: {property_name}\nRecording: {mp3_url}")
    
    base_url = str(request.base_url).rstrip("/").replace("/twilio/maintenance_recorded", "").replace("http://", "https://")
    
    final_message = f"{advice} We have logged your request for Unit {unit}. A team member will contact you shortly. Goodbye."
    thanks_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': final_message})}"

    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{thanks_url}</Play></Response>',
        media_type="application/xml",
    )

@app.api_route("/twilio/leasing_recorded", methods=["GET", "POST"])
async def leasing_recorded(request: Request):
    form = await request.form()

    from_number = form.get("From") or request.query_params.get("From")
    recording_url = form.get("RecordingUrl") or request.query_params.get("RecordingUrl")
    mp3_url = f"{recording_url}.mp3" if recording_url else ""

    logging.info(f"Leasing callback hit. From: {from_number}")

    # 1. Transcribe
    transcript = await process_audio(mp3_url)

    try:
        append_leasing_row([
            datetime.utcnow().isoformat(),
            from_number or "",
            mp3_url,
            transcript or ""
        ])
        logging.info("Sheets append successful")
    except Exception as e:
        logging.error(f"Sheets append failed: {e}")

    await slack_notify(f"🏠 Leasing voicemail from {from_number}\nTranscript: {transcript}\nRecording: {mp3_url}")

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
        range="Maintenance!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()

def append_leasing_row(values):
    creds = get_sheets_creds()
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Leasing!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()

import logging
import httpx
import json
import os
import urllib.parse
import time
from datetime import datetime

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request, BackgroundTasks
import asyncio
from fastapi.responses import Response, StreamingResponse
from openai import AsyncOpenAI
from twilio.rest import Client

app = FastAPI()
logging.basicConfig(level=logging.INFO)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
OPENAI_SECRET_KEY = os.getenv("OPENAI_SECRET_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# Initialize Twilio Client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

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
    form = await request.form()
    
    # Start recording the call immediately for quality assurance/parsing
    call_sid = form.get("CallSid")
    if twilio_client and call_sid:
        try:
            twilio_client.calls(call_sid).recordings.create()
            logging.info(f"Started recording for CallSid: {call_sid}")
        except Exception as e:
            logging.error(f"Failed to start recording: {e}")

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
        # Redirect to the start of the conversational flow
        text = "You selected leasing. Please say your full name."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        action_url = f"{base_url}/twilio/leasing/property"
        
        # Note: Gather does not support playBeep. We just prompt.
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
    <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
        <Play>{audio_url}</Play>
    </Gather>
    <Say>I did not hear anything.</Say>
    <Redirect method="POST">{base_url}/twilio/route?Digits=2</Redirect>
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

@app.api_route("/twilio/leasing/property", methods=["GET", "POST"])
async def leasing_property(request: Request):
    form = await request.form()
    base_url = str(request.base_url).rstrip("/").replace("/twilio/leasing/property", "").replace("http://", "https://")
    
    logging.info(f"Leasing Step 1 Form Keys: {list(form.keys())}")
    
    # Get Name from SpeechResult OR Query (if reprompting)
    name = form.get("SpeechResult") or request.query_params.get("name")
    
    if not name:
        logging.info("Leasing Step 1: No speech result. Reprompting.")
        text = "I didn't catch that. Please say your full name."
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        action_url = f"{base_url}/twilio/leasing/property"
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
        <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
            <Play>{audio_url}</Play>
        </Gather>
        <Say>I did not hear anything.</Say>
        <Redirect method="POST">{action_url}</Redirect>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    logging.info(f"Leasing Step 1 (Name): {name}")

    # Next Question: Property
    text = f"Thanks, {name}. Which property are you interested in?"
    audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
    
    # Pass Name to next step via URL
    next_action_url = f"{base_url}/twilio/leasing/date?name={urllib.parse.quote(name)}"
    
    # Retry this step if silence (pass name so we don't ask for it again)
    retry_url = f"{base_url}/twilio/leasing/property?name={urllib.parse.quote(name)}"
    
    # XML requires & to be escaped as &amp;
    next_action_xml = next_action_url.replace("&", "&amp;")
    retry_url_xml = retry_url.replace("&", "&amp;")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{next_action_xml}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    <Play>{audio_url}</Play>
  </Gather>
  <Say>I did not hear anything.</Say>
  <Redirect method="POST">{retry_url_xml}</Redirect>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@app.api_route("/twilio/leasing/date", methods=["GET", "POST"])
async def leasing_date(request: Request):
    form = await request.form()
    base_url = str(request.base_url).rstrip("/").replace("/twilio/leasing/date", "").replace("http://", "https://")
    
    logging.info(f"Leasing Step 2 Form Keys: {list(form.keys())}")

    name = request.query_params.get("name") or "Unknown"
    property_name = form.get("SpeechResult")
    
    if not property_name:
        logging.info("Leasing Step 2: No speech result. Reprompting.")
        text = "Sorry, which property was that?"
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        # Keep name in URL
        next_action = f"{base_url}/twilio/leasing/date?name={urllib.parse.quote(name)}"
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
        <Gather input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
            <Play>{audio_url}</Play>
        </Gather>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")

    logging.info(f"Leasing Step 2 (Property): {property_name}")

    # Next Question: Date
    text = "Got it. And when are you looking to move in?"
    audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
    
    # Pass Name & Property to next step
    next_action = f"{base_url}/twilio/leasing/finish?name={urllib.parse.quote(name)}&property={urllib.parse.quote(property_name)}"
    
    # Reload this step's URL if timeout
    current_url = f"{base_url}/twilio/leasing/date?name={urllib.parse.quote(name)}"
    
    # XML requires & to be escaped as &amp;
    next_action_xml = next_action.replace("&", "&amp;")
    current_url_xml = current_url.replace("&", "&amp;")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{next_action_xml}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    <Play>{audio_url}</Play>
  </Gather>
  <Say>I did not hear anything.</Say>
  <Redirect method="POST">{current_url_xml}</Redirect>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")



async def process_leasing_background(
    call_sid: str,
    from_number: str,
    name: str,
    property_name: str,
    move_in_date: str
):
    """Waits for audio to be ready, transcribes, and logs to Sheets."""
    logging.info(f"Background processing started for {call_sid}")
    
    # Wait for audio to be flushed/available (Twilio recordings aren't instant)
    await asyncio.sleep(10)
    
    # Clean Inputs
    name = name.strip().rstrip(".")
    property_name = property_name.strip().rstrip(".")
    move_in_date = move_in_date.strip().rstrip(".")

    # Fetch Recording URL
    audio_url = "Unknown"
    if twilio_client and call_sid != "Unknown":
        try:
            recordings = twilio_client.recordings.list(call_sid=call_sid, limit=1)
            if recordings:
                rec = recordings[0]
                audio_url = f"https://api.twilio.com{rec.uri.replace('.json', '.mp3')}"
                logging.info(f"Found recording URL: {audio_url}")
        except Exception as e:
            logging.error(f"Failed to fetch recording URL: {e}")
            audio_url = "Error fetching URL"

    # Transcribe
    full_transcript = ""
    if audio_url.startswith("http"):
        # Retry logic for download if needed, but process_audio logs errors
        full_transcript = await process_audio(audio_url) or ""
    
    if not full_transcript:
        full_transcript = f"Name: {name} | Property: {property_name} | Move-in: {move_in_date}"

    # Log to Sheets
    try:
        append_leasing_row([
            datetime.utcnow().isoformat(),
            from_number,
            name,
            property_name,
            move_in_date,
            full_transcript,
            audio_url
        ])
        logging.info("Sheets append successful (Background)")
    except Exception as e:
        logging.error(f"Sheets append failed: {e}")

@app.api_route("/twilio/leasing/finish", methods=["GET", "POST"])
async def leasing_finish(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    base_url = str(request.base_url).rstrip("/").replace("/twilio/leasing/finish", "").replace("http://", "https://")
    
    name = request.query_params.get("name") or "Unknown"
    property_name = request.query_params.get("property") or "Unknown"
    move_in_date = form.get("SpeechResult") or "Unknown"
    from_number = form.get("From") or request.query_params.get("From") or "Unknown"
    call_sid = form.get("CallSid") or "Unknown"
    
    logging.info(f"Leasing Complete: Name={name}, Property={property_name}, Date={move_in_date}")

    # Offload processing to background task
    background_tasks.add_task(
        process_leasing_background,
        call_sid,
        from_number,
        name,
        property_name,
        move_in_date
    )

    text = f"Perfect. We have {name} interested in {property_name} for {move_in_date}. A leasing agent will reach out shortly. Goodbye."
    audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
    
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{audio_url}</Play></Response>',
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

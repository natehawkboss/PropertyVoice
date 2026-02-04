import logging
import httpx
import json
import os
import urllib.parse
import time
from datetime import datetime
from typing import Optional, Literal

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request, BackgroundTasks
import asyncio
from fastapi.responses import Response, StreamingResponse
from openai import AsyncOpenAI
from twilio.rest import Client
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Feature flag: set to "conversational" or "dtmf"
CALL_FLOW_MODE = os.getenv("CALL_FLOW_MODE", "conversational")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
OPENAI_SECRET_KEY = os.getenv("OPENAI_SECRET_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# Initialize Twilio Client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

openai_client = AsyncOpenAI(api_key=OPENAI_SECRET_KEY) if OPENAI_SECRET_KEY else None


# =============================================================================
# CONVERSATION STATE MODEL & STORE
# =============================================================================

class ConversationState(BaseModel):
    """Tracks multi-turn conversation state for a call."""
    call_sid: str
    from_number: str
    intent: Literal["unknown", "maintenance", "leasing"] = "unknown"
    turn_count: int = 0
    messages: list[dict] = []  # Conversation history for LLM

    # Gathered info
    caller_name: Optional[str] = None
    unit_number: Optional[str] = None
    property_name: Optional[str] = None
    issue_description: Optional[str] = None
    move_in_date: Optional[str] = None

    is_complete: bool = False
    created_at: float = 0.0  # Unix timestamp for TTL cleanup


class ConversationStateStore:
    """In-memory state store with TTL cleanup. Interface designed for future Redis migration."""

    def __init__(self, ttl_seconds: int = 3600):
        self._store: dict[str, ConversationState] = {}
        self._ttl_seconds = ttl_seconds

    def get(self, call_sid: str) -> Optional[ConversationState]:
        """Get conversation state by call SID."""
        state = self._store.get(call_sid)
        if state and (time.time() - state.created_at) > self._ttl_seconds:
            # Expired, remove it
            del self._store[call_sid]
            return None
        return state

    def set(self, call_sid: str, state: ConversationState) -> None:
        """Store conversation state."""
        if state.created_at == 0.0:
            state.created_at = time.time()
        self._store[call_sid] = state

    def delete(self, call_sid: str) -> None:
        """Delete conversation state."""
        self._store.pop(call_sid, None)

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        now = time.time()
        expired = [
            sid for sid, state in self._store.items()
            if (now - state.created_at) > self._ttl_seconds
        ]
        for sid in expired:
            del self._store[sid]
        return len(expired)


# Global state store instance (TTL: 1 hour)
conversation_store = ConversationStateStore(ttl_seconds=3600)


# =============================================================================
# CONVERSATIONAL AI SERVICE
# =============================================================================

CONVERSATION_SYSTEM_PROMPT = """You are a friendly, empathetic property management phone assistant. You help callers with maintenance issues and leasing inquiries.

VOICE CONVERSATION RULES:
- Keep responses SHORT (1-3 sentences max) - this is a phone call, not text
- Use natural, conversational speech patterns ("Oh man", "Got it", "Hey", "Alright")
- Show empathy for problems ("That sounds frustrating", "I'm sorry to hear that")
- NEVER say "How can I help you today?" - you already know they called for help
- NEVER ask "Is there anything else?" until the conversation is complete

INTENT DETECTION:
- Infer intent naturally from what they say. Don't ask "maintenance or leasing?"
- Maintenance signs: problems, broken, leaking, not working, issue, repair, fix
- Leasing signs: apartment, rent, move in, looking for, availability, tour

FOR MAINTENANCE:
- Get: unit number, property name (if multi-property), issue description
- Express empathy, give brief safety advice if urgent (water leak, gas, fire)
- Be helpful: "Let me get that logged for you"

FOR LEASING:
- Get: caller's name, property they're interested in, desired move-in timeframe
- Be enthusiastic: "Great choice!", "That's a popular one"

OUTPUT FORMAT (JSON):
{
  "response": "What you say to the caller (1-3 sentences, conversational)",
  "intent": "maintenance" | "leasing" | "unknown",
  "extracted": {
    "caller_name": "if mentioned",
    "unit_number": "if mentioned",
    "property_name": "if mentioned",
    "issue_description": "if maintenance issue described",
    "move_in_date": "if mentioned"
  },
  "is_complete": true/false (true when you have enough info to log the request)
}

EXAMPLES:
User: "Yeah hi, my toilet won't stop running, it's been going all night"
Response: {"response": "Oh man, a running toilet all night - that's annoying and wastes water. What's your unit number so I can get maintenance out there?", "intent": "maintenance", "extracted": {"issue_description": "toilet won't stop running"}, "is_complete": false}

User: "I'm in unit 204"
Response: {"response": "Got it, unit 204. I'm logging this now - someone will reach out soon. If it gets worse, the shutoff valve is usually behind the toilet.", "intent": "maintenance", "extracted": {"unit_number": "204"}, "is_complete": true}

User: "Hi I'm looking for a 2 bedroom apartment"
Response: {"response": "Hey! We've got some great 2 bedrooms available. Which property are you interested in?", "intent": "leasing", "extracted": {}, "is_complete": false}"""


class ConversationResponse(BaseModel):
    """Response from the conversational AI."""
    response: str
    intent: Literal["maintenance", "leasing", "unknown"]
    extracted: dict = {}
    is_complete: bool = False


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    reraise=True
)
async def _conversation_ai_impl(messages: list[dict]) -> dict:
    """Internal LLM call with retry."""
    start = time.perf_counter()
    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=200,  # Keep responses short for voice
        temperature=0.7
    )
    duration = time.perf_counter() - start
    logging.info(f"[PERF] Conversation AI took {duration:.2f}s")
    return json.loads(response.choices[0].message.content)


async def process_conversation_turn(state: ConversationState, user_speech: str) -> ConversationResponse:
    """Process a single turn in the conversation and return AI response."""
    if not openai_client:
        logging.error("OpenAI client not configured")
        return ConversationResponse(
            response="I'm having trouble right now. Could you try calling back?",
            intent="unknown",
            is_complete=False
        )

    # Add user message to history
    state.messages.append({"role": "user", "content": user_speech})
    state.turn_count += 1

    # Build messages for LLM
    messages = [
        {"role": "system", "content": CONVERSATION_SYSTEM_PROMPT},
        *state.messages
    ]

    try:
        result = await _conversation_ai_impl(messages)

        # Parse response
        ai_response = ConversationResponse(
            response=result.get("response", "I didn't catch that. Could you say that again?"),
            intent=result.get("intent", "unknown"),
            extracted=result.get("extracted", {}),
            is_complete=result.get("is_complete", False)
        )

        # Add assistant response to history
        state.messages.append({"role": "assistant", "content": ai_response.response})

        # Update state with extracted info
        extracted = ai_response.extracted
        if extracted.get("caller_name"):
            state.caller_name = extracted["caller_name"]
        if extracted.get("unit_number"):
            state.unit_number = extracted["unit_number"]
        if extracted.get("property_name"):
            state.property_name = extracted["property_name"]
        if extracted.get("issue_description"):
            state.issue_description = extracted["issue_description"]
        if extracted.get("move_in_date"):
            state.move_in_date = extracted["move_in_date"]

        # Update intent if detected
        if ai_response.intent != "unknown":
            state.intent = ai_response.intent

        state.is_complete = ai_response.is_complete

        return ai_response

    except Exception as e:
        logging.error(f"Conversation AI failed: {e}")
        return ConversationResponse(
            response="I'm having trouble understanding. Could you repeat that?",
            intent=state.intent,
            is_complete=False
        )


# =============================================================================
# HEALTH ENDPOINT
# =============================================================================

@app.get("/health")
async def health():
    """Health check endpoint for Railway monitoring."""
    # Clean up expired conversations
    expired_count = conversation_store.cleanup_expired()
    if expired_count > 0:
        logging.info(f"Cleaned up {expired_count} expired conversations")

    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "mode": CALL_FLOW_MODE,
        "active_conversations": len(conversation_store._store),
        "checks": {
            "openai": openai_client is not None,
            "twilio": twilio_client is not None,
            "elevenlabs": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
        }
    }


# =============================================================================
# GOOGLE SHEETS
# =============================================================================

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

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True
)
async def _slack_notify_impl(url: str, text: str):
    """Internal implementation with retry logic."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json={"text": text})


async def slack_notify(text: str):
    """Send notification to Slack webhook with retry logic."""
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        await _slack_notify_impl(url, text)
    except Exception as e:
        logging.error(f"Slack notification failed after retries: {e}")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException, Exception)),
    reraise=True
)
async def _transcribe_audio_impl(audio_content: bytes):
    """Internal implementation for Whisper transcription with retry."""
    file_obj = ("audio.mp3", audio_content, "audio/mpeg")
    start = time.perf_counter()
    transcript = await openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=file_obj
    )
    duration = time.perf_counter() - start
    logging.info(f"[PERF] OpenAI Whisper transcription took {duration:.2f}s")
    return transcript.text


async def process_audio(recording_url: str):
    """Downloads audio and transcribes it using Whisper with retry logic."""
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

            text = await _transcribe_audio_impl(response.content)
            logging.info(f"Transcription: {text}")
            return text
    except Exception as e:
        logging.error(f"Transcription failed after retries: {e}")
        return None

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True
)
async def _classify_maintenance_impl(text: str, system_prompt: str):
    """Internal implementation for GPT classification with retry."""
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
    return json.loads(response.choices[0].message.content)


async def classify_maintenance_issue(text: str):
    """Classifies the issue and returns specific advice with retry logic."""
    if not openai_client or not text:
        return "Unknown", "Thank you. We have logged your maintenance request.", "Unknown", "Unknown"

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
        data = await _classify_maintenance_impl(text, system_prompt)
        return (
            data.get("category", "Routine"),
            data.get("advice", "We have received your request."),
            data.get("unit", "Unknown"),
            data.get("property", "Unknown")
        )
    except Exception as e:
        logging.error(f"Classification failed after retries: {e}")
        return "Error", "Thank you. We have logged your request.", "Unknown", "Unknown"

@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def voice(request: Request):
    """Main entry point for incoming calls. Routes to conversational or DTMF mode."""
    form = await request.form()
    call_sid = form.get("CallSid") or "unknown"
    from_number = form.get("From") or "unknown"

    # Start recording the call immediately for quality assurance/parsing
    if twilio_client and call_sid != "unknown":
        try:
            twilio_client.calls(call_sid).recordings.create()
            logging.info(f"Started recording for CallSid: {call_sid}")
        except Exception as e:
            logging.error(f"Failed to start recording: {e}")

    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")

    # Route based on call flow mode
    if CALL_FLOW_MODE == "conversational":
        # Initialize conversation state
        state = ConversationState(
            call_sid=call_sid,
            from_number=from_number,
            created_at=time.time()
        )
        conversation_store.set(call_sid, state)

        # Natural greeting (no menu)
        greeting = "Hey there, this is the property management line. What can I help you with today?"
        action_url = f"{base_url}/twilio/conversation?call_sid={call_sid}"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, greeting)}
  </Gather>
  <Say>I didn't hear anything. Goodbye.</Say>
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    else:
        # Legacy DTMF mode
        menu_prompt = "Hello. This is the property manager assistant. Press 1 for maintenance. Press 2 for leasing."
        no_input = "We did not receive your selection. Goodbye."

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather numDigits="1" action="{base_url}/twilio/route" method="POST">
    {tts_element(base_url, menu_prompt)}
  </Gather>
  {tts_element(base_url, no_input)}
</Response>
"""
        return Response(content=twiml, media_type="application/xml")


# =============================================================================
# CONVERSATIONAL FLOW ENDPOINT
# =============================================================================

@app.api_route("/twilio/conversation", methods=["GET", "POST"])
async def conversation(request: Request, background_tasks: BackgroundTasks):
    """Handle multi-turn conversation for both maintenance and leasing."""
    form = await request.form()
    call_sid = request.query_params.get("call_sid") or form.get("CallSid") or "unknown"
    from_number = form.get("From") or "unknown"
    user_speech = form.get("SpeechResult") or ""

    base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
    action_url = f"{base_url}/twilio/conversation?call_sid={call_sid}"

    logging.info(f"Conversation turn for {call_sid}: '{user_speech}'")

    # Get or create conversation state
    state = conversation_store.get(call_sid)
    if not state:
        # Fallback: create fresh state
        logging.warning(f"No state found for {call_sid}, creating fresh state")
        state = ConversationState(
            call_sid=call_sid,
            from_number=from_number,
            created_at=time.time()
        )

    # Handle no speech input
    if not user_speech:
        prompt = "I didn't catch that. Could you say that again?"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, prompt)}
  </Gather>
  <Say>I still didn't hear anything. Goodbye.</Say>
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    # Process the conversation turn
    ai_response = await process_conversation_turn(state, user_speech)

    # Save updated state
    conversation_store.set(call_sid, state)

    # Check if conversation is complete
    if ai_response.is_complete:
        # Finalize and log to sheets
        await _finalize_conversation(state, background_tasks)

        # Generate goodbye message
        if state.intent == "maintenance":
            goodbye = f"{ai_response.response} We've logged your request and someone will be in touch soon. Goodbye!"
        else:
            goodbye = f"{ai_response.response} A leasing agent will reach out shortly. Have a great day!"

        # Clean up state
        conversation_store.delete(call_sid)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, goodbye)}
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    # Safety: limit to 10 turns
    if state.turn_count >= 10:
        limit_msg = "Thanks for calling. If you need more help, please call back. Goodbye!"
        conversation_store.delete(call_sid)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, limit_msg)}
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    # Continue conversation
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, ai_response.response)}
  </Gather>
  <Say>I didn't hear anything. Goodbye.</Say>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")


async def _finalize_conversation(state: ConversationState, background_tasks: BackgroundTasks):
    """Log completed conversation to appropriate sheet and send notifications."""
    logging.info(f"Finalizing conversation: intent={state.intent}, call_sid={state.call_sid}")

    # Build transcript from conversation history
    transcript_parts = []
    for msg in state.messages:
        role = "Caller" if msg["role"] == "user" else "Assistant"
        transcript_parts.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_parts)

    if state.intent == "maintenance":
        try:
            append_maintenance_row([
                datetime.utcnow().isoformat(),
                state.from_number,
                "Conversation",  # Recording URL - will be available later
                transcript,
                "Conversational",  # Category
                state.issue_description or "See transcript",
                state.unit_number or "Unknown",
                state.property_name or "Unknown"
            ])
            logging.info("Maintenance conversation logged to Sheets")
        except Exception as e:
            logging.error(f"Failed to log maintenance to Sheets: {e}")

        # Slack notification
        await slack_notify(
            f"🛠️ Maintenance (Conversational) from {state.from_number}\n"
            f"Unit: {state.unit_number or 'Unknown'}\n"
            f"Property: {state.property_name or 'Unknown'}\n"
            f"Issue: {state.issue_description or 'See transcript'}\n"
            f"Transcript:\n{transcript}"
        )

    elif state.intent == "leasing":
        # Schedule background task to get recording URL later
        background_tasks.add_task(
            _process_leasing_conversation_background,
            state.call_sid,
            state.from_number,
            state.caller_name or "Unknown",
            state.property_name or "Unknown",
            state.move_in_date or "Unknown",
            transcript
        )


async def _process_leasing_conversation_background(
    call_sid: str,
    from_number: str,
    name: str,
    property_name: str,
    move_in_date: str,
    transcript: str
):
    """Background task to log leasing conversation after getting recording URL."""
    logging.info(f"Background processing leasing conversation for {call_sid}")

    audio_url = "Pending"

    # Try to get recording URL
    if twilio_client and call_sid != "unknown":
        for attempt in range(12):
            try:
                await asyncio.sleep(5)
                recordings = twilio_client.recordings.list(call_sid=call_sid, limit=1)
                if recordings and recordings[0].status == 'completed':
                    rec = recordings[0]
                    audio_url = f"https://api.twilio.com{rec.uri.replace('.json', '.mp3')}"
                    logging.info(f"Found recording URL: {audio_url}")
                    break
            except Exception as e:
                logging.error(f"Error checking recording: {e}")

    try:
        append_leasing_row([
            datetime.utcnow().isoformat(),
            from_number,
            name,
            property_name,
            move_in_date,
            transcript,
            audio_url
        ])
        logging.info("Leasing conversation logged to Sheets")
    except Exception as e:
        logging.error(f"Failed to log leasing to Sheets: {e}")


# =============================================================================
# LEGACY DTMF FLOW (kept for fallback)
# =============================================================================


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
        maint_prompt = "You selected maintenance. After the beep, please describe the issue, your unit number, and the best callback number. Press pound when finished."
        record_action = f"{base_url}/twilio/maintenance_recorded"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, maint_prompt)}
  <Record action="{record_action}" method="POST" maxLength="120" finishOnKey="#" playBeep="true" />
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    elif digit == "2":
        # Redirect to the start of the conversational flow
        leasing_prompt = "You selected leasing. Please say your full name."
        action_url = f"{base_url}/twilio/leasing/property"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, leasing_prompt)}
  </Gather>
  <Say>I did not hear anything.</Say>
  <Redirect method="POST">{base_url}/twilio/route?Digits=2</Redirect>
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    else:
        invalid_msg = "Invalid selection. Goodbye."
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, invalid_msg)}
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

@app.get("/twilio_say")
async def twilio_say(text: str):
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">{text}</Say>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True
)
async def _elevenlabs_tts_impl(text: str):
    """Internal ElevenLabs TTS call with retry."""
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
            raise httpx.HTTPStatusError(
                f"ElevenLabs error {r.status_code}",
                request=r.request,
                response=r
            )

        return r.content


def elevenlabs_available() -> bool:
    """Check if ElevenLabs TTS is configured."""
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def tts_element(base_url: str, text: str) -> str:
    """Generate TwiML element for TTS - <Play> for ElevenLabs, <Say> for fallback."""
    if elevenlabs_available():
        audio_url = f"{base_url}/tts?{urllib.parse.urlencode({'text': text})}"
        return f'<Play>{audio_url}</Play>'
    else:
        # Escape XML special characters in text
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<Say voice="Polly.Joanna">{escaped}</Say>'


@app.get("/tts")
async def tts(text: str):
    """Text-to-speech endpoint using ElevenLabs."""
    logging.info("TTS requested: %s", text)

    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logging.error("ElevenLabs not configured - this endpoint requires ElevenLabs")
        return Response("TTS not configured", status_code=503)

    try:
        audio_content = await _elevenlabs_tts_impl(text)
        return StreamingResponse(iter([audio_content]), media_type="audio/mpeg")
    except Exception as e:
        logging.error(f"ElevenLabs TTS failed after retries: {e}")
        return Response("TTS unavailable", status_code=503)

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

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, final_message)}
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@app.api_route("/twilio/leasing/property", methods=["GET", "POST"])
async def leasing_property(request: Request):
    form = await request.form()
    base_url = str(request.base_url).rstrip("/").replace("/twilio/leasing/property", "").replace("http://", "https://")
    
    logging.info(f"Leasing Step 1 Form Keys: {list(form.keys())}")
    
    # Get Name from SpeechResult OR Query (if reprompting)
    name = form.get("SpeechResult") or request.query_params.get("name")
    
    if not name:
        logging.info("Leasing Step 1: No speech result. Reprompting.")
        prompt = "I didn't catch that. Please say your full name."
        action_url = f"{base_url}/twilio/leasing/property"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, prompt)}
  </Gather>
  <Say>I did not hear anything.</Say>
  <Redirect method="POST">{action_url}</Redirect>
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    logging.info(f"Leasing Step 1 (Name): {name}")

    # Next Question: Property
    prompt = f"Thanks, {name}. Which property are you interested in?"

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
    {tts_element(base_url, prompt)}
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
        prompt = "Sorry, which property was that?"
        next_action = f"{base_url}/twilio/leasing/date?name={urllib.parse.quote(name)}"

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto" speechModel="phone_call">
    {tts_element(base_url, prompt)}
  </Gather>
</Response>
"""
        return Response(content=twiml, media_type="application/xml")

    logging.info(f"Leasing Step 2 (Property): {property_name}")

    # Next Question: Date
    prompt = "Got it. And when are you looking to move in?"

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
    {tts_element(base_url, prompt)}
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
    
    # Wait for audio to be flushed/available (retry loop)
    # Twilio recording processing can take time. We poll for 'completed' status.
    audio_url = "Unknown"
    
    # Clean Inputs
    name = name.strip().rstrip(".")
    property_name = property_name.strip().rstrip(".")
    move_in_date = move_in_date.strip().rstrip(".")
    
    if twilio_client and call_sid != "Unknown":
        for attempt in range(12): # Poll for up to 60 seconds (12 * 5s)
            try:
                await asyncio.sleep(5)
                recordings = twilio_client.recordings.list(call_sid=call_sid, limit=1)
                if recordings:
                    rec = recordings[0]
                    if rec.status == 'completed':
                        audio_url = f"https://api.twilio.com{rec.uri.replace('.json', '.mp3')}"
                        logging.info(f"Found completed recording URL: {audio_url}")
                        break
                    else:
                        logging.info(f"Recording status: {rec.status}. Retrying...")
                else:
                    logging.info("No recordings found yet. Retrying...")
            except Exception as e:
                logging.error(f"Error checking recording status: {e}")
                
        if audio_url == "Unknown":
            logging.error("Timed out waiting for recording to complete.")

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

    goodbye_msg = f"Perfect. We have {name} interested in {property_name} for {move_in_date}. A leasing agent will reach out shortly. Goodbye."

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {tts_element(base_url, goodbye_msg)}
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True
)
def append_maintenance_row(values):
    """Append a row to the Maintenance sheet with retry logic."""
    creds = get_sheets_creds()
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Maintenance!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True
)
def append_leasing_row(values):
    """Append a row to the Leasing sheet with retry logic."""
    creds = get_sheets_creds()
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Leasing!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()

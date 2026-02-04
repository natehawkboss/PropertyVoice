# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PropertyVoice is an AI-powered phone receptionist for property management. It handles inbound calls via Twilio with two operating modes:
- **Conversational AI mode** (default): Natural multi-turn conversation powered by GPT-4o-mini
- **DTMF mode** (legacy): Traditional phone menu with number keypresses

The system transcribes audio with OpenAI Whisper, and logs everything to Google Sheets.

## Commands

**Run locally:**
```bash
uvicorn main:app --reload
```

**Run tests:**
```bash
pytest tests/
```

**Production (Railway):**
```bash
uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'
```

## Architecture

**Single-file monolith:** All code lives in `main.py`. This is intentional for simplicity.

### Call Flow Modes

#### Conversational Mode (default, `CALL_FLOW_MODE=conversational`)

```
/twilio/voice → Natural greeting, speech gather
    ↓
/twilio/conversation → Multi-turn AI conversation
    ↓
[Intent detected: maintenance or leasing]
    ↓
LLM extracts: name, unit, property, issue, move-in date
    ↓
is_complete=true → Log to Sheets, send notifications
```

**Key components:**
- `ConversationState` - Pydantic model tracking call state
- `ConversationStateStore` - In-memory state store with TTL
- `process_conversation_turn()` - Processes speech and returns AI response
- `CONVERSATION_SYSTEM_PROMPT` - Instructs LLM for empathetic, short responses

#### DTMF Mode (legacy, `CALL_FLOW_MODE=dtmf`)

```
/twilio/voice → Gather DTMF (1=Maintenance, 2=Leasing)
    ↓
/twilio/route → Routes based on digit pressed
    ↓
[Maintenance Path]
    → Record voicemail → /twilio/maintenance_recorded
    → Transcribe (Whisper) → Classify (GPT-4o-mini) → Sheets + Slack

[Leasing Path]
    → /twilio/leasing/property (ask name)
    → /twilio/leasing/date (ask property)
    → /twilio/leasing/finish (ask move-in date)
    → Background task: poll recording → transcribe → Sheets
```

### Key Patterns

- **Async-first:** All external API calls use async/await (httpx, OpenAI, etc.)
- **Retry with tenacity:** External API calls have exponential backoff retries
- **TwiML responses:** Endpoints return `application/xml` with Twilio Markup Language
- **Recording polling:** Background tasks poll Twilio for recording completion
- **Graceful degradation:**
  - ElevenLabs unavailable → Falls back to Twilio `<Say>` (Polly.Joanna voice)
  - State store miss → Creates fresh conversation state
  - LLM error → Returns fallback "Could you repeat that?" response
  - Slack webhook missing → Silently skips notifications
- **BackgroundTasks:** Leasing uses FastAPI background tasks for non-blocking processing

### External Services

| Service | Purpose |
|---------|---------|
| Twilio | Voice calls, recordings, speech recognition |
| OpenAI | Whisper (transcription), GPT-4o-mini (conversation/classification) |
| ElevenLabs | Text-to-speech (optional, with Twilio fallback) |
| Google Sheets | Data persistence (Maintenance + Leasing tabs) |
| Slack | Notifications (optional) |

## Environment Variables

**Required:**
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- `OPENAI_SECRET_KEY`
- `GOOGLE_SHEET_ID`, `GOOGLE_CREDS_JSON` (full JSON string, not file path)

**Optional:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (TTS - falls back to Twilio if not set)
- `SLACK_WEBHOOK_URL` (notifications)
- `CALL_FLOW_MODE` - Set to `conversational` (default) or `dtmf` (legacy)

See `.env.example` for template. Local dev variables are in `dev.env`.

## Data Storage

No database. Google Sheets is the single source of truth with two tabs:
- **Maintenance:** Timestamp, Caller ID, Recording URL, Transcript, Category, Advice, Unit, Property
- **Leasing:** Timestamp, Caller ID, Name, Property Interest, Move-in Date, Transcript, Audio URL

## Testing

Tests are in `tests/` using pytest and pytest-asyncio:
- `tests/conftest.py` - Fixtures and mocked environment
- `tests/test_twiml.py` - TwiML generation tests
- `tests/test_call_flow.py` - Integration tests for call flow endpoints

## Health Endpoint

`GET /health` returns:
- Service status
- Active call flow mode
- Active conversation count
- Service connectivity checks (OpenAI, Twilio, ElevenLabs)

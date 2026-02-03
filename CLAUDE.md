# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PropertyVoice is an AI-powered phone receptionist for property management. It handles inbound calls via Twilio, routes callers to maintenance (voicemail) or leasing (conversational) flows, transcribes audio with OpenAI Whisper, classifies issues with GPT-4o-mini, and logs everything to Google Sheets.

## Commands

**Run locally:**
```bash
uvicorn main:app --reload
```

**Production (Railway):**
```bash
uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'
```

No test suite or linter is configured.

## Architecture

**Single-file monolith:** All code lives in `main.py` (~512 lines). This is intentional for simplicity.

### Call Flow State Machine

Twilio webhooks drive a stateless flow where state passes via URL query parameters:

```
/twilio/voice → Gather DTMF (1=Maintenance, 2=Leasing)
    ↓
/twilio/route → Routes based on digit pressed
    ↓
[Maintenance Path]
    → Record voicemail → /twilio/maintenance_recorded
    → Transcribe (Whisper) → Classify (GPT-4o-mini) → Sheets + Slack

[Leasing Path]
    → /twilio/leasing/property (ask name, speech input)
    → /twilio/leasing/date (ask property interest)
    → /twilio/leasing/finish (ask move-in date)
    → Background task: poll recording → transcribe → Sheets
```

### Key Patterns

- **Async-first:** All external API calls use async/await (httpx, OpenAI, etc.)
- **TwiML responses:** Endpoints return `application/xml` with Twilio Markup Language
- **Recording polling:** Leasing flow polls Twilio every 5 seconds (up to 60s) for recording completion before transcription
- **Graceful degradation:** Missing ElevenLabs key falls back to Twilio `<Say>`; missing Slack webhook silently skips notifications
- **BackgroundTasks:** Leasing uses FastAPI background tasks for non-blocking processing

### External Services

| Service | Purpose |
|---------|---------|
| Twilio | Voice calls, recordings, DTMF routing |
| OpenAI | Whisper (transcription), GPT-4o-mini (classification) |
| ElevenLabs | Text-to-speech (optional) |
| Google Sheets | Data persistence (Maintenance + Leasing tabs) |
| Slack | Notifications (optional) |

## Environment Variables

**Required:**
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- `OPENAI_SECRET_KEY`
- `GOOGLE_SHEET_ID`, `GOOGLE_CREDS_JSON` (full JSON string, not file path)

**Optional:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (TTS)
- `SLACK_WEBHOOK_URL` (notifications)

Local dev variables are in `dev.env`.

## Data Storage

No database. Google Sheets is the single source of truth with two tabs:
- **Maintenance:** Timestamp, Caller ID, Unit, Property, Issue Type, Transcript, Audio URL
- **Leasing:** Timestamp, Caller ID, Name, Property Interest, Move-in Date, Transcript, Audio URL

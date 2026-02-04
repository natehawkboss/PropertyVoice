"""Pytest fixtures and configuration for PropertyVoice tests."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Set environment variables before importing main
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test_sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_token")
os.environ.setdefault("OPENAI_SECRET_KEY", "test_key")
os.environ.setdefault("GOOGLE_SHEET_ID", "test_sheet_id")
os.environ.setdefault("GOOGLE_CREDS_JSON", '{"type":"service_account","project_id":"test"}')
os.environ.setdefault("ELEVENLABS_API_KEY", "test_elevenlabs_key")
os.environ.setdefault("ELEVENLABS_VOICE_ID", "test_voice_id")
os.environ.setdefault("CALL_FLOW_MODE", "conversational")


@pytest.fixture
def mock_env(monkeypatch):
    """Set up mock environment variables."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "test_sid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setenv("OPENAI_SECRET_KEY", "test_key")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "test_sheet_id")
    monkeypatch.setenv("GOOGLE_CREDS_JSON", '{"type":"service_account","project_id":"test"}')
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test_elevenlabs_key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test_voice_id")
    monkeypatch.setenv("CALL_FLOW_MODE", "conversational")


@pytest.fixture
def mock_twilio_client():
    """Mock Twilio client."""
    with patch("main.twilio_client") as mock:
        mock.calls.return_value.recordings.create.return_value = MagicMock()
        mock.recordings.list.return_value = []
        yield mock


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client."""
    with patch("main.openai_client") as mock:
        # Mock transcription
        mock.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text="Test transcription")
        )
        # Mock chat completion
        mock.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content='{"category":"Routine","advice":"We will handle this.","unit":"101","property":"Test Property"}'
                        )
                    )
                ]
            )
        )
        yield mock


@pytest.fixture
def mock_httpx():
    """Mock httpx client for external API calls."""
    with patch("main.httpx.AsyncClient") as mock:
        client_instance = AsyncMock()
        mock.return_value.__aenter__.return_value = client_instance
        mock.return_value.__aexit__.return_value = None

        # Default successful response
        client_instance.get.return_value = MagicMock(
            status_code=200,
            content=b"fake audio content"
        )
        client_instance.post.return_value = MagicMock(
            status_code=200,
            content=b"fake tts audio",
            text=""
        )
        yield client_instance


@pytest.fixture
def mock_sheets():
    """Mock Google Sheets API."""
    with patch("main.build") as mock_build:
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {}
        yield mock_service


@pytest.fixture
def app_client():
    """Create test client for the FastAPI app."""
    # Import here to ensure mocked env vars are set
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)

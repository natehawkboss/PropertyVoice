"""Tests for TwiML response generation."""

import pytest
from fastapi.testclient import TestClient


def test_health_endpoint(app_client):
    """Test that health endpoint returns correct structure."""
    response = app_client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data
    assert "mode" in data
    assert "checks" in data
    assert "openai" in data["checks"]
    assert "twilio" in data["checks"]
    assert "elevenlabs" in data["checks"]


def test_twilio_say_endpoint(app_client):
    """Test the Twilio Say TwiML endpoint."""
    response = app_client.get("/twilio_say", params={"text": "Hello world"})
    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]

    content = response.text
    assert '<?xml version="1.0"' in content
    assert "<Response>" in content
    assert "<Say" in content
    assert "Hello world" in content


def test_twiml_contains_xml_declaration():
    """Test that TwiML responses contain proper XML declaration."""
    from main import twiml_play

    result = twiml_play("https://example.com/audio.mp3")
    assert '<?xml version="1.0" encoding="UTF-8"?>' in result
    assert "<Response>" in result
    assert "<Play>" in result
    assert "https://example.com/audio.mp3" in result


def test_twiml_play_function():
    """Test the twiml_play helper function."""
    from main import twiml_play

    url = "https://test.com/audio.mp3"
    result = twiml_play(url)

    assert '<?xml version="1.0" encoding="UTF-8"?>' in result
    assert "<Response>" in result
    assert f"<Play>{url}</Play>" in result
    assert "</Response>" in result

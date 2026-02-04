"""Integration tests for the call flow endpoints."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestVoiceEndpoint:
    """Tests for /twilio/voice endpoint."""

    def test_voice_returns_twiml(self, app_client, mock_twilio_client):
        """Test that voice endpoint returns valid TwiML."""
        response = app_client.post(
            "/twilio/voice",
            data={"CallSid": "CA123", "From": "+15551234567"}
        )

        assert response.status_code == 200
        assert "application/xml" in response.headers["content-type"]

        content = response.text
        assert '<?xml version="1.0"' in content
        assert "<Response>" in content


class TestRouteEndpoint:
    """Tests for /twilio/route endpoint."""

    def test_route_digit_1_maintenance(self, app_client):
        """Test routing to maintenance flow with digit 1."""
        response = app_client.post(
            "/twilio/route",
            data={"Digits": "1", "CallSid": "CA123"}
        )

        assert response.status_code == 200
        content = response.text
        assert "<Record" in content
        assert "maintenance_recorded" in content

    def test_route_digit_2_leasing(self, app_client):
        """Test routing to leasing flow with digit 2."""
        response = app_client.post(
            "/twilio/route",
            data={"Digits": "2", "CallSid": "CA123"}
        )

        assert response.status_code == 200
        content = response.text
        assert "<Gather" in content
        assert 'input="speech"' in content

    def test_route_invalid_digit(self, app_client):
        """Test handling of invalid digit."""
        response = app_client.post(
            "/twilio/route",
            data={"Digits": "9", "CallSid": "CA123"}
        )

        assert response.status_code == 200
        content = response.text
        assert "<Response>" in content


class TestMaintenanceFlow:
    """Tests for maintenance recording flow."""

    @pytest.mark.asyncio
    async def test_maintenance_recorded_processes_audio(
        self, app_client, mock_openai_client, mock_httpx, mock_sheets
    ):
        """Test that maintenance_recorded endpoint processes the recording."""
        with patch("main.get_sheets_creds") as mock_creds:
            mock_creds.return_value = MagicMock()

            response = app_client.post(
                "/twilio/maintenance_recorded",
                data={
                    "From": "+15551234567",
                    "RecordingUrl": "https://api.twilio.com/recording",
                    "CallSid": "CA123"
                }
            )

            assert response.status_code == 200
            assert "application/xml" in response.headers["content-type"]


class TestLeasingFlow:
    """Tests for leasing conversational flow."""

    def test_leasing_property_asks_for_name(self, app_client):
        """Test that leasing/property asks for property when name provided."""
        response = app_client.post(
            "/twilio/leasing/property",
            data={"SpeechResult": "John Smith", "CallSid": "CA123"}
        )

        assert response.status_code == 200
        content = response.text
        assert "<Gather" in content
        assert "leasing/date" in content

    def test_leasing_property_reprompts_without_speech(self, app_client):
        """Test that leasing/property reprompts when no speech detected."""
        response = app_client.post(
            "/twilio/leasing/property",
            data={"CallSid": "CA123"}  # No SpeechResult
        )

        assert response.status_code == 200
        content = response.text
        assert "<Gather" in content
        # Should still be on property endpoint for retry

    def test_leasing_date_asks_for_date(self, app_client):
        """Test that leasing/date asks for move-in date."""
        response = app_client.post(
            "/twilio/leasing/date?name=John%20Smith",
            data={"SpeechResult": "Parkview Apartments", "CallSid": "CA123"}
        )

        assert response.status_code == 200
        content = response.text
        assert "<Gather" in content
        assert "leasing/finish" in content

    def test_leasing_finish_completes_flow(self, app_client, mock_twilio_client):
        """Test that leasing/finish completes the flow."""
        response = app_client.post(
            "/twilio/leasing/finish?name=John%20Smith&property=Parkview",
            data={
                "SpeechResult": "next month",
                "From": "+15551234567",
                "CallSid": "CA123"
            }
        )

        assert response.status_code == 200
        content = response.text
        assert "<Response>" in content
        assert "<Play>" in content

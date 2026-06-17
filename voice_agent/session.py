"""
VoiceAgentSession - manages a single Deepgram Voice Agent connection for one phone call.

This is the core of the audio pipeline.  It bridges two WebSocket connections:

  Twilio WebSocket  ←→  VoiceAgentSession  ←→  Deepgram Voice Agent API

Audio flow:
  1. Twilio sends mulaw audio as base64 JSON → we decode → send raw bytes to Deepgram
  2. Deepgram sends raw mulaw bytes back   → we encode to base64 → send JSON to Twilio

The session also handles:
  - Barge-in (sending Twilio "clear" events when the user starts speaking)
  - Function call dispatch (routing to backend/scheduling_service.py)
  - Transcript logging
  - Lifecycle management

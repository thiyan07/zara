"""Shared pytest bootstrap: make the SUITE environment deterministic
without touching production behavior.

- HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE: local STT models are CACHED
  (faster-whisper tiny.en, 75 MB, verified snapshot). Without this
  pin, faster-whisper's hub check can wait on an unresponsive network
  path and hang whole runs (observed 2026-10-19, two full-suite runs
  stuck >10 min at test_agent_voice_turn_mock_turn_and_interrupt on a
  model_info TCP connect). Offline mode makes the cache-hit path the
  only path; any genuinely missing model fails FAST and loudly
  instead of hanging.
- ZARA_DISCOVERY_OFF=1 (existing guard): do not run host discovery
  daemons from build_stack in tests.
"""
import os

os.environ.setdefault("ZARA_DISCOVERY_OFF", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Phase 11B — Fish Audio FRIDAY Voice Configuration

Provider: Fish Audio (https://fish.audio/developers/)
Endpoint: POST https://api.fish.audio/v1/tts

Selected voice: FRIDAY
Reference ID: 8ec7352282684afc90cdfef961cbdae7
Model: S2.1 Pro (existing preferred; not reinvented)
Format: mp3

Configuration:
- config/config.yaml: fish_audio.reference_id
- veronica/config.py: TTSConfig.fish_audio_reference_id
- config/.env: FISH_AUDIO_REFERENCE_ID=8ec7352282684afc90cdfef961cbdae7
- Secret API key (FISH_AUDIO_API_KEY) kept only in secret management (.env); never in source/docs/logs.

Constraints preserved:
- Voice is output-only; does not affect reasoning/planner/memory/permissions/verification/autonomy/computer-use.
- No Phase 11C (listening/VAD/continuous loop) implemented.
- No second TTS system created; existing TTS abstraction reused.

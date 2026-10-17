"""
Typed configuration loading for VERONICA.

Design goals:
- ONE place that knows how to find settings (YAML for non-secrets, .env for secrets).
- No module anywhere else reads environment variables directly — everything goes
  through `Config`, so switching a provider/model/key is always a one-file change.
- Sensible, working defaults with NO required paid API key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / "config" / ".env"


@dataclass
class LLMConfig:
    provider: str = "ollama"            # "ollama" | "openai_compatible" | "anthropic" | "echo"
    vision_model: Optional[str] = None  # model for "what's on my screen" (default: model)
    # A vision model on this PC (Ollama) for when the cloud one can't be reached. None = off.
    offline_vision_model: Optional[str] = None
    offline_base_url: str = "http://localhost:11434"
    anthropic_effort: str = "low"       # Claude only: low | medium | high | xhigh | max (low = fastest)
    model: str = "llama3.1:8b"
    base_url: str = "http://localhost:11434"
    api_key_env: str = "VERONICA_LLM_API_KEY"   # only read if provider needs a key
    temperature: float = 0.4
    max_history_turns: int = 12
    request_timeout_s: int = 60
    voice_num_predict: int = 96
    # Another provider for when this one fails (e.g. Groq while Gemini is overloaded):
    # {provider, model, base_url, api_key_env}. None = no backup.
    backup: Optional[dict] = None


@dataclass
class STTConfig:
    backend: str = "google_free"        # "auto" | "groq_whisper" | "google_free" | "vosk" | "faster_whisper" | "none"
    # "auto": Groq Whisper (most accurate) -> Google -> offline faster-whisper, in that order.
    groq_whisper_model: str = "whisper-large-v3-turbo"
    groq_api_key_env: str = "GROQ_API_KEY"
    no_speech_reject: float = 0.6       # Whisper: drop a clip it thinks is probably not speech
    offline_whisper_model: str = "base" # local fallback when there's no internet
    vosk_model_path: str = "models/vosk-model-small-en-us"
    faster_whisper_model: str = "base"  # "tiny" | "base" | "small" | "medium" | "large-v3" (faster-whisper)
    faster_whisper_compute_type: str = "float32"  # "float16" (CUDA) | "int8" | "float32"
    language: str = "en-IN"
    energy_threshold: int = 300
    pause_threshold: float = 0.8
    # Phase 11F — capture via VERONICA's sounddevice MicrophoneInput (no PyAudio needed)
    mic_backend: str = "sounddevice"    # "sounddevice" | "pyaudio" (google_free only, if installed)
    mic_device_index: Optional[int] = None  # fallback if mic_device_name isn't found
    mic_device_name: Optional[str] = None   # e.g. "Microphone Array"; wins over the index
    min_speech_rms: float = 120.0       # int16 RMS floor for "speech started"; lower it for a quiet mic
    max_utterance_s: float = 8.0
    # -- accuracy & noise (2026-10-16) --
    highpass_hz: float = 100.0          # cut fan/AC hum below this before recognition (0 = off)
    min_voiced_s: float = 0.25          # shorter bursts (a click, a cough) are not speech
    confirm_below_confidence: float = 0.45  # after the wake word: ask "did you say...?" below this (0 = never)
    corrections_path: str = "data/stt_corrections.json"  # "when I say X I mean Y", taught by voice


@dataclass
class TTSConfig:
    backend: str = "edge_tts"           # "edge_tts" | "kokoro" | "piper" | "pyttsx3" | "none" | "fish_audio" | "elevenlabs"
    voice: str = "en-US-AriaNeural"
    rate: str = "+0%"
    # When the main voice can't speak (e.g. ElevenLabs credits used up): a free Microsoft
    # neural voice via edge-tts, then Windows' own voice — so she never goes silent.
    backup_voice: str = "en-GB-SoniaNeural"
    backup_minutes: float = 180         # after a credits/quota refusal, use the backup this long
    piper_model: str = "en_US-lessac-medium"  # piper voice model name
    fish_audio_reference_id: Optional[str] = None  # Fish Audio voice reference (FRIDAY = 8ec...ae7)
    fish_audio_model: str = "s2.1-pro-free"  # Fish Audio free developer model (fix 402)
    fish_audio_format: str = "mp3"
    # ElevenLabs (backend "elevenlabs"); API key is ELEVENLABS_API_KEY in config/.env.
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model: str = "eleven_flash_v2_5"      # fastest; "eleven_multilingual_v2" = richer, slower
    elevenlabs_output_format: str = "mp3_44100_128"
    cache_dir: str = "data/tts_cache"
    kokoro_speed: float = 1.0           # backend "kokoro": 0.8 slower .. 1.3 faster


@dataclass
class VoiceConfig:
    enabled: bool = False                  # master switch (Step 3)
    continuous_listening: bool = False      # master continuous loop (Step 3)
    wake_words: list[str] = field(default_factory=lambda: ["Veronica", "Vero"])
    listen_timeout_s: float = 10.0
    max_utterance_s: float = 8.0
    silence_duration_s: float = 1.0
    return_to_listen: bool = True
    suppress_playback_during_capture: bool = True
    barge_in_enabled: bool = False
    barge_in_threshold: float = 0.02
    barge_in_min_speech_ms: int = 300
    barge_in_guard_ms: int = 300
    barge_in_cooldown_ms: int = 500
    # 11E.1 — real audio backend (master switches stay false; opt-in only)
    mic_backend: str = "sounddevice"       # "sounddevice" | "pyaudio"
    mic_device_index: int | None = None     # null = default
    mic_sample_rate: int = 16000
    mic_chunk_size: int = 1024
    mic_channels: int = 1
    wake_backend: str = "openwakeword"      # "openwakeword" | "mock"
    wake_model_path: str = "models/veronica_wake_model.onnx"  # BLOCKED until verified
    wake_threshold: float = 0.5             # openWakeWord score needed to wake (used by --wake)
    follow_up_seconds: float = 0.0          # conversation mode: listen this long after a reply, no wake word
    conversation_mode: bool = False         # stay in conversation (no wake word) until an ending phrase
    conversation_idle_timeout_s: float = 300  # safety: drop back to the wake word after this much silence (0 = never)
    # In an open conversation (no wake word this turn), ignore speech that isn't clearly
    # aimed at her: below this recognition confidence, or not this many times louder
    # than the room (a TV across the room is quieter than you at the mic).
    conversation_min_confidence: float = 0.6
    conversation_speech_ratio: float = 3.0
    barge_in: str = "wake_word"             # interrupt her while she talks: "wake_word" | "off"
    shared_mic: bool = True                 # one always-open mic stream (faster, no clipped first words)


@dataclass
class WakeWordConfig:
    enabled: bool = False
    mode: str = "push_to_talk"          # "push_to_talk" | "energy_spike"
    spike_ratio: float = 3.0


@dataclass
class MemoryConfig:
    long_term_path: str = "data/memory.json"
    short_term_max_messages: int = 20
    # Keep the recent conversation across restarts (off by default; config.yaml turns it on).
    persist_conversation: bool = False
    conversation_path: str = "data/conversation.json"
    resume_hours: float = 12.0
    include_facts_in_prompt: bool = False  # send remembered facts + home city with each question
    max_prompt_facts: int = 40             # at most this many (newest kept)


@dataclass
class MemoryThreadConfig:
    """Thread Memory V1 (persistent episodic/semantic memory).

    ``enabled`` defaults to False so an untouched config keeps VERONICA behaviour-identical
    and Thread simply isn't wired in. Turning it on points Thread at the JSONL record store
    and the (replaceable) vector backend.
    """
    enabled: bool = False
    records_path: str = "data/thread_memory.jsonl"
    vector_path: str = "data/thread_vectors.jsonl"
    vector_backend: str = "local"            # "local" (pure-Python) | future: "chromadb"/...
    embedding_provider: str = "local"        # "local" (deterministic) | future providers
    max_context_memories: int = 5            # how many retrieved memories enter the prompt
    retrieval_threshold: float = 0.20        # minimum relevance-scaled score to be included
    consolidation_enabled: bool = True       # deterministic consolidation on writes when on
    auto_consolidate: bool = False           # run consolidation after each write (off: manual)
    # Episodic Memory Integration V1: how many past *experiences* may enter the prompt as
    # RETRIEVED EXPERIENCE context (bounded context budget, spec Phase 22). 0 disables
    # experience injection entirely while keeping Thread episode recording live.
    episode_context_limit: int = 3


@dataclass
class ModelEntryConfig:
    """One routable model as declared in config.yaml under ``routing.models``."""
    model_id: str
    provider: str                        # "ollama" | "openai_compatible" | "echo"
    role: str = "general"                # general | fast | reasoning | coding | vision | backup | offline
    capabilities: tuple[str, ...] = ()   # extensible (e.g. ("tools", "vision"))
    task_types: tuple[str, ...] = ()     # empty = general-purpose
    context_window: int = 8192
    supports_tools: bool = False
    supports_vision: bool = False
    supports_streaming: bool = False
    speed_class: str = "medium"          # fast | medium | slow
    reasoning_class: str = "none"        # none | light | heavy
    cost_class: str = "local"            # local | free | low | medium | high
    priority: int = 100                  # lower = preferred
    enabled: bool = True
    default: bool = False                # mark ONE model as the routing default
    # Per-model construction overrides (optional; otherwise global llm.* applies).
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None
    timeout_s: int = 60


_GODS_EYE_DEFAULT_TOOLS = [
    "aircraft_in_area", "find_aircraft", "get_aircraft_info", "satellites_overhead", "next_satellite_pass",
    "get_earthquakes", "get_recent_launches", "find_cctv_cameras", "get_cctv_snapshot", "get_weather_map",
    "get_cyclones", "situation_brief", "vessels_in_area", "show_in_gods_eye_view",
]


@dataclass
class DownloadConfig:
    """Downloading files from a direct link (tools/download_tools.py). Asks every time."""
    dir: str = ""              # where to save; empty = the user's Downloads folder
    max_mb: float = 2048       # refuse a single file bigger than this (0 = no limit)


@dataclass
class PrivacyConfig:
    """What VERONICA scrubs from everything sent to the cloud model (safety/privacy.py).
    On by default; her own words still go to the model (that is how she answers you), but
    secrets and sensitive IDs never leave the machine."""
    enabled: bool = True
    redact_financial: bool = True          # card numbers (Luhn), CVV/PIN, account/routing/IFSC
    redact_government_ids: bool = True      # SSN, Aadhaar, PAN
    log_redactions: bool = True             # note locally that something was removed
    extra_patterns: tuple = ()              # your own regexes to redact


@dataclass
class CodingConfig:
    """'Learn X' study notes and 'write me a script' (tools/learning_tools.py)."""
    enabled: bool = True
    knowledge_path: str = "data/knowledge.json"
    projects_dir: str = ""        # empty = Documents\VERONICA Projects
    editor: str = ""              # app name to open code in; empty = VS Code if installed, else Notepad
    run_timeout_s: int = 60
    max_fix_attempts: int = 2     # automatic fix-and-rerun attempts after a failed run


@dataclass
class ProactiveConfig:
    """VERONICA speaking up on her own (core/proactive.py). Off by default."""
    enabled: bool = False
    check_every_s: float = 60
    battery: bool = True
    performance: bool = True
    disk: bool = True
    rain: bool = True
    greet_on_open: bool = True                # greeting + time + weather + headlines when you open the laptop
    greet_cooldown_minutes: float = 60        # at most one greeting per this many minutes
    away_minutes: float = 15                  # idle this long, then active again = "came back"
    break_after_minutes: float = 120          # 0 = never suggest a break
    quiet_start_hour: int = 23                # only urgent alerts between these hours
    quiet_end_hour: int = 7


@dataclass
class GodsEyeConfig:
    """God's Eye View integration (live public spatial data + 3D globe). Off by default."""
    enabled: bool = False
    path: str = "external/gods-eye-view"
    base_url: str = "http://localhost:4173"
    auto_start: bool = True                  # start the app's server hidden when VERONICA starts
    default_radius_km: float = 100.0         # "planes over me" = within this distance
    tools: list[str] = field(default_factory=lambda: list(_GODS_EYE_DEFAULT_TOOLS))


@dataclass
class UnderstandingConfig:
    """core/understanding.py + core/grounding.py. Off unless config.yaml turns it on."""
    enabled: bool = False
    ground_facts: bool = True        # look factual questions up (Wikipedia) and check figures


@dataclass
class Mem0Config:
    """Mem0 (memory/mem0_memory.py): facts picked up from conversation. Off unless config.yaml turns it on."""
    enabled: bool = False
    model: Optional[str] = None     # None = llm.model (Flash-Lite)
    recall_k: int = 5


@dataclass
class BrowserAgentConfig:
    """browser-use (integrations/browser_agent.py): web tasks in VERONICA's own browser."""
    enabled: bool = True
    headless: bool = False          # False = you can watch her browse
    model: Optional[str] = None     # None = llm.model (Flash-Lite: one call per step)
    max_steps: int = 25


@dataclass
class GpsConfig:
    """GPS (integrations/gps.py): a USB GPS found on a COM port, or the user's phone sending
    NMEA over UDP (turned on by voice) or serving it over TCP ("host:port")."""
    enabled: bool = True
    scan_serial: bool = True
    udp_port: int = 10110
    tcp: str = ""


@dataclass
class WorldMonitorConfig:
    """World Monitor, self-hosted with Docker (integrations/world_monitor.py). Off by default."""
    enabled: bool = False
    path: str = "external/worldmonitor"
    base_url: str = "http://localhost:3000"
    key_env: str = "WORLDMONITOR_KEY"        # operator key; also in World Monitor's own .env
    auto_start: bool = True                  # docker compose up -d in the background when needed
    seed_every_minutes: float = 60           # refresh its data from public sources (0 = never)
    max_tools: int = 40
    tools: list[str] = field(default_factory=list)   # empty = every tool it offers


@dataclass
class RoutingConfig:
    """Multi-Model Routing V1. ``enabled=False`` (default) keeps VERONICA on the
    classic single-provider path — byte-identical behaviour."""
    enabled: bool = False
    models: list[ModelEntryConfig] = field(default_factory=list)
    fallback_enabled: bool = True
    fallback_on_empty_response: bool = True
    max_fallback_attempts: int = 2
    health_ttl_s: float = 30.0
    log_routing_decisions: bool = True


@dataclass
class StreamingConfig:
    """Streaming LLM V1 (defaults: DISABLED so an untouched config keeps VERONICA
    behaviour-identical — the classic non-streaming chat() path).

    ``enabled`` is the master switch. ``preferred`` streams every turn that the
    selected model supports (False = stream only when the user explicitly asks, i.e.
    the TaskAnalyzer flags requires_streaming). ``max_response_chars`` caps the
    accumulator (explicit truncated=True at the cap — never silent truncation).
    ``max_buffer_chars``/``coalesce_ms`` tune the SSE delta cadence (never the final
    text). The three timeout_s values map to first-token / idle / overall; 0 disables
    each.
    """
    enabled: bool = False
    preferred: bool = False               # stream on every turn the model supports
    max_response_chars: int = 8192        # accumulator cap (0 = uncapped)
    max_buffer_chars: int = 1024          # emit-buffer cap between deltas
    coalesce_ms: int = 60                 # minimum interval between delta events (0 = per-chunk)
    first_token_timeout_s: float = 30.0   # nothing received within this -> FAILED timeout
    idle_timeout_s: float = 60.0          # silence between deltas -> FAILED timeout
    overall_timeout_s: float = 300.0      # hard ceiling on one streaming turn
    speak_while_writing: bool = True      # voice mode: speak each sentence as soon as it's written


@dataclass
class SafetyConfig:
    require_confirmation_above: str = "MODERATE"   # PermissionLevel name
    dry_run: bool = False


@dataclass
class LearningConfig:
    enabled: bool = True
    experience_path: str = "data/experiences.jsonl"   # JSONL experience store
    confidence_path: str = "data/confidence.json"     # optional confidence persistence
    # Phase 4 — controlled behavioral adaptation. Conservative defaults: adaptation only
    # fires at high confidence with repeated evidence, and is fully disabled whenever
    # learning.enabled is False (enforced at the wiring layer).
    adaptation_enabled: bool = True
    adaptation_min_confidence: float = 0.80   # below this: no adaptation at all
    adaptation_strong_confidence: float = 0.90  # at/above this: strong (if evidence met)
    adaptation_min_evidence: int = 3         # repeated independent successes required for strong
    # Phase 5 — learning effectiveness & strategy evolution (observational, deterministic).
    metrics_enabled: bool = True              # compute analytics from stored experiences
    strategy_evolution_enabled: bool = True   # track strategy states and evolution
    evidence_weak: int = 2                    # 2+ successes/recoveries → PROMISING
    evidence_normal: int = 3                  # 3+ → eligible for normal confidence
    evidence_strong: int = 5                  # 5+ → eligible for VALIDATED
    evidence_high: int = 10                   # 10+ → high-evidence strategy


@dataclass
class ContextConfig:
    """Context Window Management V1 configuration.
    ``enabled`` is the master switch; when False the ContextManager is still
    constructed but the orchestrator does not call it, preserving byte-identical
    behaviour with the legacy message assembly."""
    enabled: bool = False
    default_context_window: int = 8192
    compression_enabled: bool = True
    max_context_items: int = 64
    inline_tools_in_prompt: bool = False
    output_reserve: int = 1024
    safety_reserve: int = 256
    budget_system: int = 2000
    budget_conversation: int = 3000
    budget_memory: int = 1200
    budget_tools: int = 1500
    budget_observation: int = 800
    safety_instruction: Optional[str] = None


@dataclass
class VisionConfig:
    """Vision Model V1 configuration.
    ``enabled`` is the master switch; when False the vision provider is not used,
    preserving byte-identical behaviour with legacy perception-only path.
    ``provider`` specifies which VisionProvider implementation to use.
    ``model`` is the model name or identifier for the provider.
    ``timeout_seconds`` is the maximum time allowed for a vision analysis job.
    """
    enabled: bool = False
    provider: str = "stub"  # "stub", "ollama", "openai_compatible", etc.
    model: str = "moondream:latest"
    timeout_seconds: float = 30.0
    base_url: str = "http://localhost:11434"
    # Privacy settings are handled by VisionImagePrivacy, which reads from this config
    crop_to_active_window: bool = True
    downscale_width: int = 640
    downscale_height: int = 480
    to_grayscale: bool = False
    full_screen_fallback: bool = False
    # VisionPolicy gating thresholds
    cooldown_seconds: float = 5.0
    change_threshold: float = 0.1
    importance_threshold: float = 0.3
    consider_task_relevance: bool = True
    task_relevance_threshold: float = 0.4


@dataclass
class ObservationConfig:
    """Continuous Screen Perception V1 configuration.
    ``enabled`` is the master switch; when False the ContinuousObservationService
    is not wired in, preserving byte-identical behaviour."""
    enabled: bool = False
    interval_seconds: float = 2.0
    change_strategy: str = "ACTIVE"  # IDLE, ACTIVE, WAITING, ACTION, ERROR
    max_queue_size: int = 50
    max_recent_events: int = 100
    task_aware_enabled: bool = True
    screen_capture_enabled: bool = True
    uia_enabled: bool = True
    ocr_enabled: bool = False  # OCR is heavy; opt-in
    graceful_degradation: bool = True


@dataclass
class AssistantConfig:
    name: str = "VERONICA"
    persona: str = (
        "You are VERONICA, a capable, calm, and precise personal AI assistant "
        "inspired by JARVIS. You are concise, proactive, and never pretend to have "
        "done something you have not actually done. When a tool is available for a "
        "task, you use it instead of guessing."
    )
    tool_guide: str = ""                 # operational tool rules, kept apart from the character
    wake_phrase: str = "veronica"
    # Default city for weather/briefings. Saying "my city is X" stores one in
    # data/profile.json, which takes precedence over this.
    home_city: Optional[str] = None
    auto_detect_location: bool = False   # guess the city from the public IP when none is set
    news_region: str = "IN"              # Google News edition (country code), e.g. IN, US, GB
    news_world_region: str = "US"        # edition used for WORLD headlines (international English);
                                         # general news = home-country first, then world headlines
    default_browser: Optional[str] = None  # e.g. "Brave"; None = the Windows default browser
    default_play_service: Optional[str] = None  # where "play X" plays; None = YouTube
    search_engine: Optional[str] = None  # default engine for "search/browse X"; None = Google
    search_url: Optional[str] = None     # its results URL with {query}; None = reach it by name
    news_language: str = "en"


@dataclass
class Config:
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    memory_thread: MemoryThreadConfig = field(default_factory=MemoryThreadConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    gods_eye: GodsEyeConfig = field(default_factory=GodsEyeConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    coding: CodingConfig = field(default_factory=CodingConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    world_monitor: WorldMonitorConfig = field(default_factory=WorldMonitorConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    browser_agent: BrowserAgentConfig = field(default_factory=BrowserAgentConfig)
    mem0: Mem0Config = field(default_factory=Mem0Config)
    understanding: UnderstandingConfig = field(default_factory=UnderstandingConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    log_level: str = "INFO"
    log_file: str = "data/veronica.log"
    # "conversation": the terminal shows only the user's and VERONICA's words (logs and
    # status lines still go to log_file). "full": everything, for troubleshooting.
    console_mode: str = "full"

    # ---- secrets, read once via .env, never scattered around the codebase ----
    _secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def secret(self, env_var: str, default: str | None = None) -> str | None:
        return self._secrets.get(env_var, os.environ.get(env_var, default))

    def resolve_path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _merge_dataclass(instance: Any, overrides: dict[str, Any]) -> Any:
    for key, value in overrides.items():
        if not hasattr(instance, key):
            # Nested mapping: YAML may use top-level names that map to nested
            # dataclass fields (e.g. fish_audio.reference_id -> tts.fish_audio_reference_id).
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    nested_name = f"{key}_{sub_key}" if isinstance(instance, TTSConfig) else sub_key
                    # Map fish_audio.* to tts.fish_audio_* when instance is Config
                    if isinstance(instance, Config) and key == "fish_audio" and hasattr(instance.tts, f"fish_audio_{sub_key}"):
                        target_key = f"fish_audio_{sub_key}"
                        nested = getattr(instance.tts, target_key, None)
                        if nested is not None or hasattr(instance.tts, target_key):
                            if hasattr(nested, "__dataclass_fields__") and isinstance(sub_value, dict):
                                _merge_dataclass(nested, sub_value)
                            else:
                                setattr(instance.tts, target_key, sub_value)
                        else:
                            # Fallback: set directly on instance.tts using mapped key
                            if hasattr(instance.tts, target_key):
                                setattr(instance.tts, target_key, sub_value)
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def _coerce_routing_models(cfg: Config) -> None:
    """Convert raw ``routing.models`` YAML dicts into typed ModelEntryConfig objects.
    Runs after the generic merge so a list of dicts becomes a list of typed entries."""
    models = cfg.routing.models
    if not models:
        return
    known = set(ModelEntryConfig.__dataclass_fields__)
    converted: list[ModelEntryConfig] = []
    for item in models:
        if isinstance(item, ModelEntryConfig):
            converted.append(item)
            continue
        if isinstance(item, dict):
            converted.append(
                ModelEntryConfig(**{k: v for k, v in item.items() if k in known})
            )
        else:
            raise TypeError(
                f"routing.models entries must be dicts or ModelEntryConfig, "
                f"got {type(item).__name__}"
            )
    cfg.routing.models = converted


def load_config(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    env_path: Path | str = DEFAULT_ENV_PATH,
) -> Config:
    """Load YAML config (non-secret settings) + .env (secrets), return a typed Config."""
    load_dotenv(dotenv_path=env_path, override=False)

    cfg = Config()
    config_path = Path(config_path)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _merge_dataclass(cfg, raw)
    _coerce_routing_models(cfg)

    # Pull anything that looks like a secret out of the environment once, up front.
    for key in (
        "VERONICA_LLM_API_KEY",
        "OPENWEATHER_API_KEY",
        "OPENROUTER_API_KEY",
        "SERPAPI_API_KEY",
        "FISH_AUDIO_API_KEY",
        "FISH_AUDIO_REFERENCE_ID",
    ):
        val = os.environ.get(key)
        if val:
            cfg._secrets[key] = val

    return cfg

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
    provider: str = "ollama"            # "ollama" | "openai_compatible" | "echo"
    model: str = "llama3.1:8b"
    base_url: str = "http://localhost:11434"
    api_key_env: str = "VERONICA_LLM_API_KEY"   # only read if provider needs a key
    temperature: float = 0.4
    max_history_turns: int = 12
    request_timeout_s: int = 60


@dataclass
class STTConfig:
    backend: str = "google_free"        # "google_free" | "vosk" | "faster_whisper" | "none"
    vosk_model_path: str = "models/vosk-model-small-en-us"
    faster_whisper_model: str = "base"  # "tiny" | "base" | "small" | "medium" | "large-v3" (faster-whisper)
    faster_whisper_compute_type: str = "float32"  # "float16" (CUDA) | "int8" | "float32"
    language: str = "en-IN"
    energy_threshold: int = 300
    pause_threshold: float = 0.8


@dataclass
class TTSConfig:
    backend: str = "edge_tts"           # "edge_tts" | "piper" | "pyttsx3" | "none"
    voice: str = "en-US-AriaNeural"
    rate: str = "+0%"
    piper_model: str = "en_US-lessac-medium"  # piper voice model name
    cache_dir: str = "data/tts_cache"


@dataclass
class WakeWordConfig:
    enabled: bool = False
    mode: str = "push_to_talk"          # "push_to_talk" | "energy_spike"
    spike_ratio: float = 3.0


@dataclass
class MemoryConfig:
    long_term_path: str = "data/memory.json"
    short_term_max_messages: int = 20


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
    role: str = "general"                # general | fast | reasoning | coding | vision
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
    wake_phrase: str = "veronica"


@dataclass
class Config:
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    memory_thread: MemoryThreadConfig = field(default_factory=MemoryThreadConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    log_level: str = "INFO"
    log_file: str = "data/veronica.log"

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
    ):
        val = os.environ.get(key)
        if val:
            cfg._secrets[key] = val

    return cfg

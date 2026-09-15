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
from typing import Any

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
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
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

"""
Entry point: `python -m veronica.main [--voice | --wake] [--dry-run] [--config PATH]`

Wires together config -> logging -> LLM provider -> tool registry (with permission
gate) -> orchestrator -> CLI front-end. This is the only place all those pieces are
constructed together, so alternate front-ends (a GUI, a scheduled agent) can reuse the
same wiring by calling `build_app()` instead of duplicating it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from veronica.config import Config, load_config
from veronica.core.capability import build_default_capabilities
from veronica.core.orchestrator import Orchestrator
from veronica.llm.base import LLMProvider
from veronica.llm.router import build_provider
from veronica.logging_setup import get_logger, setup_logging
from veronica.memory.long_term import LongTermMemory
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.registry import ToolRegistry, build_default_registry
from veronica.automation import MultiStepPlanner
from veronica.automation.task_executor import TaskExecutor
from veronica.learning import (
    ConfidenceTracker,
    ExperienceStore,
    LearningEngine,
    LearningMetrics,
    StrategyEvolution,
)
from veronica.learning.policy import PolicyThresholds, SAFETY_SENSITIVE_TOOLS
from veronica.learning.adaptation import BehavioralAdapter
from veronica.ui.cli import CLIApp, _confirm_in_terminal


@dataclass
class App:
    config: Config
    llm: LLMProvider
    tools: ToolRegistry
    orchestrator: Orchestrator
    # Learning & Adaptation v1 (optional, fault-tolerant): experience store + engine the
    # task executor reports into. Exposed so front-ends/tests can inspect/reset them.
    learning_store: ExperienceStore | None = None
    learning_engine: LearningEngine | None = None
    # Phase 5 (optional): deterministic analytics + strategy evolution over the store.
    learning_metrics: LearningMetrics | None = None
    strategy_evolution: StrategyEvolution | None = None
    # Phase 7 (optional): persistent episodic/semantic Thread Memory. None when disabled
    # or when the build failed (VERONICA still fully operates without it).
    thread_memory: object | None = None  # ThreadMemory, typed as object to avoid circular import at App level
    # Episodic Memory Integration V1 (optional): consumes the executor's authoritative
    # Experience into Thread episodes; retrieves prior experiences as prompt context;
    # tracks procedure evidence. All None when Thread is disabled — identical behaviour
    # to pre-episodic builds.
    episode_recorder: object | None = None
    episode_retriever: object | None = None
    procedure_store: object | None = None
    # Continuous Screen Perception V1 (optional): background desktop observation service.
    # None when disabled or build failed — VERONICA operates identically without it.
    observation_service: object | None = None


def build_app(config: Config, interactive: bool = True) -> App:
    setup_logging(config.log_level, config.resolve_path(config.log_file))
    logger = get_logger("main")
    logger.info("Booting %s (llm.provider=%s, llm.model=%s)", config.assistant.name, config.llm.provider, config.llm.model)

    llm = build_provider(config)

    # ---- Multi-Model Routing V1 (optional, opt-in) ----
    routing_assembly = None
    if config.routing.enabled:
        try:
            from veronica.llm.routing.builder import build_routing
            routing_assembly = build_routing(config)
            logger.info(
                "Model routing enabled (%d model(s) registered)",
                len(routing_assembly.registry),
            )
        except Exception:  # noqa: BLE001 — routing must never prevent VERONICA from booting
            logger.exception("Model routing build failed; falling back to single-model (llm.provider=%s).",
                             config.llm.provider)
            routing_assembly = None

    # ---- Streaming LLM V1 (optional, opt-in via config.streaming.enabled) ----
    # One StreamController for every stream that flows through the app. It only
    # sequences/accumulates chunks and emits display events — it never plans, routes,
    # permissions, or writes memory (the orchestrator stays authoritative). When
    # disabled, Orchestrator.handle() runs unchanged and handle_stream() falls back.
    stream_controller = None
    if config.streaming.enabled:
        try:
            from veronica.llm.streaming import StreamController, StreamingTimeouts
            cfg_stream = config.streaming
            stream_controller = StreamController(
                timeouts=StreamingTimeouts(
                    first_token_s=cfg_stream.first_token_timeout_s,
                    idle_s=cfg_stream.idle_timeout_s,
                    overall_s=cfg_stream.overall_timeout_s,
                ),
                coalesce_ms=cfg_stream.coalesce_ms,
                max_buffer_chars=cfg_stream.max_buffer_chars,
            )
            logger.info(
                "Streaming LLM enabled (coalesce_ms=%d, buffer=%d, cap=%s, "
                "first_token=%ss, idle=%ss, overall=%ss)",
                cfg_stream.coalesce_ms, cfg_stream.max_buffer_chars,
                cfg_stream.max_response_chars,
                cfg_stream.first_token_timeout_s, cfg_stream.idle_timeout_s,
                cfg_stream.overall_timeout_s,
            )
        except Exception:  # noqa: BLE001 — streaming must never prevent VERONICA from booting
            logger.exception("Streaming controller build failed; continuing non-streaming.")
            stream_controller = None

    # ---- Context Window Management V1 (optional, opt-in via config.context.enabled) ---
    # The dedicated context selection layer. When enabled, the orchestrator routes
    # message assembly through it (falling back to the legacy path on any failure); when
    # disabled it stays unwired — byte-identical behaviour. The manager is constructed
    # regardless so wiring is uniform; the orchestrator only consults it when enabled.
    context_manager = None
    if getattr(config.context, "enabled", False):
        try:
            from veronica.core.context_manager import ContextManager
            context_manager = ContextManager.from_config(config)
            logger.info(
                "Context Window Management V1 enabled (window=%d, budgets: conv=%d mem=%d tools=%d, "
                "compression=%s)",
                context_manager.default_context_window,
                context_manager._budget_fields["conversation"],
                context_manager._budget_fields["memory"],
                context_manager._budget_fields["tools"],
                context_manager.compression_enabled,
            )
        except Exception:  # noqa: BLE001 — context management must never block boot
            logger.exception("Context manager build failed; continuing with legacy assembly.")
            context_manager = None

    gate = PermissionGate(
        threshold=PermissionLevel[config.safety.require_confirmation_above.upper()],
        confirm_callback=_confirm_in_terminal if interactive else None,
        dry_run=config.safety.dry_run,
    )
    tools = build_default_registry(gate, config=config, llm=llm)

    long_term_memory = LongTermMemory(config.resolve_path(config.memory.long_term_path))

    # Phase 7: persistent Thread Memory. OPTIONAL — when disabled in config or when the
    # build fails (e.g. filesystem issue), VERONICA still operates with short/long-term
    # memory only. Thread must never be the reason the assistant fails to start.
    thread_memory = None
    if config.memory_thread.enabled:
        try:
            from veronica.memory.thread.thread_memory import ThreadMemory as _ThreadMem
            thread_memory = _ThreadMem.build(
                config=config.memory_thread,
                records_path=config.resolve_path(config.memory_thread.records_path),
                vector_path=config.resolve_path(config.memory_thread.vector_path),
                vector_backend=config.memory_thread.vector_backend,
                embedding_backend=config.memory_thread.embedding_provider,
            )
            logger.info(
                "Thread Memory enabled (records=%s, vector=%s, provider=%s)",
                config.memory_thread.records_path,
                config.memory_thread.vector_path,
                config.memory_thread.embedding_provider,
            )
        except Exception:  # noqa: BLE001 — Thread must never prevent VERONICA from booting
            logger.exception("Thread Memory build failed; proceeding without it.")
            thread_memory = None

    # Episodic Memory Integration V1 (optional, follows Thread): the recorder persists
    # each authoritative Experience into a Thread episode; the retriever pulls prior
    # experiences back as labeled context; the procedure store tracks repeated-work
    # evidence. All three degrade to no-ops on any failure — memory never breaks a task.
    episode_recorder = None
    episode_retriever = None
    procedure_store = None
    if thread_memory is not None:
        try:
            from veronica.memory.thread.episodes import EpisodeRecorder, EpisodeRetriever, ProcedureStore
            episode_recorder = EpisodeRecorder(thread_memory)
            episode_retriever = EpisodeRetriever(
                thread_memory,
                default_top_k=config.memory_thread.episode_context_limit,
            )
            procedure_store = ProcedureStore(thread_memory)
            logger.info(
                "Episodic Memory Integration V1 enabled (experience context limit=%d)",
                config.memory_thread.episode_context_limit,
            )
        except Exception:  # noqa: BLE001 — episodic memory must never block the build
            logger.exception("Episodic memory component build failed; continuing without it.")
            episode_recorder = None
            episode_retriever = None
            procedure_store = None

    # ---- Continuous Screen Perception V1 (optional, opt-in via config.observation.enabled) ----
    # Background desktop observation service. When enabled, runs a worker thread that continuously
    # captures screen state, detects changes, filters importance, and emits events. When disabled
    # it stays unwired — byte-identical behaviour. Fault-tolerant: any failure is logged and
    # the service simply isn't available (observation_service = None).
    observation_service = None
    if getattr(config.observation, "enabled", False):
        try:
            from veronica.vision.continuous_observer import (
                ContinuousObservationService,
                ObservationConfig,
                create_continuous_observation_service,
            )
            # Convert config to ObservationConfig
            obs_config = ObservationConfig(
                enabled=config.observation.enabled,
                interval_seconds=config.observation.interval_seconds,
                change_strategy=config.observation.change_strategy,
                max_queue_size=config.observation.max_queue_size,
                max_recent_events=config.observation.max_recent_events,
                task_aware_enabled=config.observation.task_aware_enabled,
                screen_capture_enabled=config.observation.screen_capture_enabled,
                uia_enabled=config.observation.uia_enabled,
                ocr_enabled=config.observation.ocr_enabled,
                graceful_degradation=config.observation.graceful_degradation,
            )
            observation_service = create_continuous_observation_service(config=obs_config)
            logger.info(
                "Continuous Screen Perception V1 enabled (interval=%.1fs, strategy=%s, queue=%d, history=%d)",
                obs_config.interval_seconds,
                obs_config.change_strategy,
                obs_config.max_queue_size,
                obs_config.max_recent_events,
            )
        except Exception:  # noqa: BLE001 — observation must never prevent VERONICA from booting
            logger.exception("Continuous observation service build failed; continuing without it.")
            observation_service = None

    # Agentic brain: goal → planned steps → verified execution. Both stay injectable so
    # front-ends can hand the SAME executor to the Stitch bridge for live task progress.
    # Learning & Adaptation v1: the executor reports an Experience at every terminal task
    # state. It is optional and fault-tolerant — a store/engine failure is logged and
    # swallowed, never allowed to fail the task.
    learning_store = ExperienceStore(config.resolve_path(config.learning.experience_path))
    learning_engine = LearningEngine(
        confidence=ConfidenceTracker(path=config.resolve_path(config.learning.confidence_path))
    )

    # Phase 4: Controlled behavioral adaptation. Creates a BehavioralAdapter configured
    # with policy thresholds from the learning config. The adapter is only used when
    # adaptation_enabled is True in the config.
    behavioral_adapter = None
    if config.learning.enabled and config.learning.adaptation_enabled:
        policy_thresholds = PolicyThresholds(
            min_confidence=config.learning.adaptation_min_confidence,
            strong_confidence=config.learning.adaptation_strong_confidence,
            min_evidence=config.learning.adaptation_min_evidence,
        )
        behavioral_adapter = BehavioralAdapter(thresholds=policy_thresholds)
        logger.info(
            "Behavioral adaptation enabled (min_conf=%.2f, strong_conf=%.2f, min_evidence=%d)",
            config.learning.adaptation_min_confidence,
            config.learning.adaptation_strong_confidence,
            config.learning.adaptation_min_evidence,
        )

    # ONE authoritative source of task outcome: the executor's Experience is consumed
    # here by BOTH systems that learn from it — the Learning engine (when enabled) and
    # Thread episodic memory (when present). None of these consumers can fail the task:
    # every one is fault-isolated downstream.
    def _on_experience(exp) -> None:
        if config.learning.enabled:
            learning_store.record(exp)
            learning_engine.process(exp)
        if episode_recorder is not None:
            episode_recorder.record(exp)
            if procedure_store is not None:
                # Evidence for repeated procedures — derived from the same episode the
                # recorder just built, purely advisory (see ProcedureStore).
                episode = episode_recorder.last_episode
                if episode is not None:
                    procedure_store.update_from(episode)

    task_executor = TaskExecutor(
        tools=tools,
        experience_callback=_on_experience
        if (config.learning.enabled or thread_memory is not None)
        else None,
    )
    planner = MultiStepPlanner()

    # Phase 5 — learning effectiveness & strategy evolution. Both are pure observers
    # over the experience store: metrics are derived from stored experiences (no second
    # source of truth) and strategy evolution applies deterministic evidence rules.
    # They never execute tools or bypass permissions.
    learning_metrics = None
    strategy_evolution = None
    if config.learning.enabled:
        store_for_metrics = learning_store
        if config.learning.metrics_enabled:
            learning_metrics = LearningMetrics(store_for_metrics)
            logger.info(
                "Learning analytics enabled (metrics=%s, strategy_evolution=%s)",
                config.learning.metrics_enabled,
                config.learning.strategy_evolution_enabled,
            )
        if config.learning.strategy_evolution_enabled:
            if store_for_metrics is not None:
                strategy_evolution = StrategyEvolution(
                    store_for_metrics,
                    metrics=learning_metrics if learning_metrics is not None else None,
                )

    orchestrator = Orchestrator(
        config=config,
        llm=llm,
        tools=tools,
        long_term_memory=long_term_memory,
        capabilities=build_default_capabilities(),
        planner=planner,
        task_executor=task_executor,
        learning_store=learning_store if config.learning.enabled else None,
        learning_engine=learning_engine if config.learning.enabled else None,
        behavioral_adapter=behavioral_adapter,
        thread_memory=thread_memory,
        router=routing_assembly.router if routing_assembly is not None else None,
        analyzer=routing_assembly.analyzer if routing_assembly is not None else None,
        episode_recorder=episode_recorder,
        episode_retriever=episode_retriever,
        procedure_store=procedure_store,
        stream_controller=stream_controller,
        context_manager=context_manager,
    )
    return App(
        config=config,
        llm=llm,
        tools=tools,
        orchestrator=orchestrator,
        learning_store=learning_store,
        learning_engine=learning_engine,
        learning_metrics=learning_metrics,
        strategy_evolution=strategy_evolution,
        thread_memory=thread_memory,
        episode_recorder=episode_recorder,
        episode_retriever=episode_retriever,
        procedure_store=procedure_store,
        observation_service=observation_service,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="VERONICA — a JARVIS-inspired personal AI assistant.")
    parser.add_argument("--voice", action="store_true", help="Enable voice input/output.")
    parser.add_argument("--wake", action="store_true",
                        help="Voice mode that waits for the wake word before each request (implies --voice).")
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate risky actions instead of executing them.")
    args = parser.parse_args()

    from veronica.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH
    config = load_config(config_path=args.config or DEFAULT_CONFIG_PATH, env_path=DEFAULT_ENV_PATH)
    if args.dry_run:
        config.safety.dry_run = True

    app = build_app(config, interactive=True)
    cli = CLIApp(config=config, orchestrator=app.orchestrator,
                 voice_mode=args.voice or args.wake, wake_mode=args.wake)
    cli.run()


if __name__ == "__main__":
    main()

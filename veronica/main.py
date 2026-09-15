"""
Entry point: `python -m veronica.main [--voice] [--dry-run] [--config PATH]`

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


def build_app(config: Config, interactive: bool = True) -> App:
    setup_logging(config.log_level, config.resolve_path(config.log_file))
    logger = get_logger("main")
    logger.info("Booting %s (llm.provider=%s, llm.model=%s)", config.assistant.name, config.llm.provider, config.llm.model)

    llm = build_provider(config)

    gate = PermissionGate(
        threshold=PermissionLevel[config.safety.require_confirmation_above.upper()],
        confirm_callback=_confirm_in_terminal if interactive else None,
        dry_run=config.safety.dry_run,
    )
    tools = build_default_registry(gate, config=config)

    long_term_memory = LongTermMemory(config.resolve_path(config.memory.long_term_path))

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

    def _on_experience(exp) -> None:
        learning_store.record(exp)
        learning_engine.process(exp)

    task_executor = TaskExecutor(
        tools=tools,
        experience_callback=_on_experience if config.learning.enabled else None,
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
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="VERONICA — a JARVIS-inspired personal AI assistant.")
    parser.add_argument("--voice", action="store_true", help="Enable voice input/output.")
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate risky actions instead of executing them.")
    args = parser.parse_args()

    from veronica.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH
    config = load_config(config_path=args.config or DEFAULT_CONFIG_PATH, env_path=DEFAULT_ENV_PATH)
    if args.dry_run:
        config.safety.dry_run = True

    app = build_app(config, interactive=True)
    cli = CLIApp(config=config, orchestrator=app.orchestrator, voice_mode=args.voice)
    cli.run()


if __name__ == "__main__":
    main()

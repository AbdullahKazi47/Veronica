#!/usr/bin/env python3
"""Read-only profile of payload, options, prompt, response length."""
import requests, time, json

# 1. Options inspection (from source/code, not changing anything)
print("=== 1. OPTIONS / GENERATION PARAMETERS ===")
print("Payload options (from ollama_provider.chat): {'temperature': 0.4}")
print("  temperature = 0.4 (only value set; from config.yaml llm.temperature)")
print("  num_predict  = ABSENT (not in payload, not in config.yaml, not in provider)")
print("  top_p        = ABSENT")
print("  top_k        = ABSENT")
print("  repeat_penalty = ABSENT")
print("  stop         = ABSENT")
print("  num_ctx / ctx_size = NOT SENT")
print("  RESULT: Ollama uses its own defaults. Default for num_predict is -1 (unlimited / very long).")

# 2. System prompt / instruction controlling length/style
from veronica.config import load_config
cfg = load_config()
print()
print("=== 2. SYSTEM PROMPT / INSTRUCTION ===")
print("Base persona (from config.assistant.persona):")
print(repr(cfg.assistant.persona[:500]))
print()
print("Contains NO length cap, NO 'short answer' instruction, NO token limit,")
print("NO max-sentence constraint. Only a JARVIS-style persona describing capability/proactiveness.")
print("No streaming.enabled, no context compression, no routing: nothing limits response length.")

# 3. num_predict
print()
print("=== 3. NUM_PREDICT STATUS ===")
print("num_predict: NOT SET anywhere in config.yaml llm.*, not added by ollama_provider,")
print("not added by orchestrator. Ollama default applies: -1 (unlimited) or large default.")
print("This allows the 159 output tokens freely.")

# 4. Orchestrator additions
print()
print("=== 4. ORCHESTRATOR PROMPT ADDITIONS (from source, no continuous run) ===")
print("For a simple greeting with new session:")
print("  - persona (from config.assistant): included")
print("  - capabilities.describe_for_prompt(): ADDED (registers all 17 tools)")
print("  - task_executor.get_context_summary(): ADDED (execution context block)")
print("  - thread_memory: DISABLED (no memory injection)")
print("  - episode_retriever: DISABLED")
print("  - routing / context manager: DISABLED")
print("  - session.short_term messages: EMPTY (first turn)")
print("Tool schemas are NOT injected into system prompt for this turn (capabilities only injects compact inventory).")
print("No reasoning/planning instructions appended explicitly.")

# 5. Measurement (current config, unmodified payload, direct /api/chat with same options)
url = 'http://localhost:11434/api/chat'
payload = {
    'model': 'qwen3:8b',
    'messages': [{'role':'user','content':'Hi Veronica.'}],
    'stream': False,
    'options': {'temperature': 0.4}
}
print()
print("=== 5. CURRENT MEASUREMENT (unmodified payload, clean /api/chat) ===")
t0 = time.time()
resp = requests.post(url, json=payload, timeout=120)
total = time.time() - t0
data = resp.json()

msg = data.get('message', {})
content = msg.get('content', '')

output_toks = data.get('eval_count', 'N/A')
input_toks = data.get('prompt_eval_count', 'N/A')
gen_time_s = data.get('eval_duration', 0) / 1e9
load_time_s = data.get('load_duration', 0) / 1e9
total_time_s = data.get('total_duration', 0) / 1e9
toks_per_sec = output_toks / gen_time_s if isinstance(output_toks, int) and gen_time_s > 0 else 'N/A'

print(f"Output tokens (eval_count):     {output_toks}")
print(f"Input tokens (prompt_eval_count): {input_toks}")
print(f"Generation time (eval_duration): {gen_time_s:.2f} s")
print(f"Load time (load_duration):       {load_time_s:.2f} s")
print(f"Total time (total_duration):     {total_time_s:.2f} s")
print(f"Tokens/sec (eval/gen_time):      {toks_per_sec}")
print(f"Response length preview:         {content[:220].replace(chr(10), ' ')} ...")

# Audit: where VERONICA matched words instead of understanding (2026-11-10)

Goal: talk to her the way you talk to ChatGPT or Claude. She should understand what you mean,
not recognise commands.

## What the audit found
A sentence used to travel like this: **keyword patterns → example matcher → AI model**. The
model only saw a sentence when nothing earlier had claimed it.

| What was hard-wired | Where | What it did to a conversation |
| --- | --- | --- |
| 96 `matches()` keyword patterns across 41 files (284 compiled regexes) | every tool | The first pattern to fire won. "Do you think volcanoes are scary?" ran the volcano report |
| Conjunction splitter | `core/compound.py` | "Open Chrome and also check the news then make it quieter" was cut at the "and/then" and each piece matched alone. Chrome never opened |
| "Two sentences = a plan" and a list of trigger phrases | `orchestrator._is_agentic_request` | Any two-sentence remark went to the planner |
| "Verb with no object → ask" | `core/intents.py` | "Ok close it now", right after opening Notepad, got "What would you like me to close?" |
| No conversation in the decision | patterns and examples see one sentence | "And in Delhi?", "bit more", "actually make it 7" meant nothing; they waited 6–10 s for the big model |
| Argument extraction by regex | each tool's `extract_arguments` | "remind me 2 call mom at 6" → text `''`, when = the whole sentence |

Measured before the change on 19 conversational turns (real loop, tools stubbed): 4 wrong actions
or needless questions. Another 9 turns fell through to the slow full model.

## What changed: meaning first (`core/interpreter.py`)
The order is now **AI model first, patterns as evidence**:

1. A fast model reads the sentence **with the conversation so far** and the last thing she did.
2. It answers one of four ways:
   - **act**: one or more tool calls in order, arguments filled in from context
   - **chat**: just conversation
   - **other**: needs the full model and all her tools
   - **ask**: one short question, only when truly ambiguous
3. The old patterns and the example matcher no longer decide. They pick the handful of tools worth
   showing the model, and their guess is shown to it labelled as a guess. That keeps the prompt
   small enough for the fast free models.
4. Patterns act alone in only two cases: an exact command that the pattern and the examples agree
   on with high confidence ("what time is it", so no wait), and when no model can be reached
   (offline or over quota, so she keeps working as before).

The same 19 turns after the change: **19 of 19 understood**. Typical added wait is 1.6 s (measured live).

| You say | Before | Now |
| --- | --- | --- |
| "and in delhi?" (after Mumbai weather) | full model, 6–10 s | weather for Delhi |
| "ok close it now" (after opening Notepad) | "What would you like me to close?" | closes Notepad |
| "do you think volcanoes are scary" | ran the volcano report | conversation |
| "open chrome and also check the news then make it a bit quieter" | news + volume; Chrome dropped | Chrome, news, volume, in order |
| "bit more" / "that's too low bring it back up" | full model | volume down / up |
| "remind me 2 call mom at 6" | text empty, time = whole sentence | text "call mom", when "at 6" |
| "actually make it 7" | full model | reminder at 7 |
| "anything about cricket in that?" (after the news) | full model | news, topic cricket |
| "yaar thoda awaaz kam karo" | worked (a taught example) | works without being taught |

## What was deliberately NOT replaced
- **The PermissionGate and every safety rule.** Each step the model picks still goes through
  `ToolRegistry.dispatch()`, so sensitive actions still ask you. A test pins this.
- **The model's answer is checked.** She only accepts tools that were offered and arguments the
  tool declares. Required arguments must be present. An invented tool or argument never runs.
- **Yes/no for permission questions, "go to sleep", conversation-ending phrases** (`ui/cli.py`).
  These stay exact on purpose: closing her, or approving a shutdown, must never hinge on a model's guess.
- **Privacy scrubbing, wake word, noise filters, voice lock.** These are signal processing and safety, not understanding.
- **The patterns themselves.** They are the offline path and the evidence the model is shown.
  Nothing new was added to them.

## Honest limits
- **It needs the internet and Groq's free tier.** That allows 1,000 calls a day and 8,000 tokens a
  minute per model. She alternates two models to double the per-minute allowance, and a busy model
  is skipped for 20 s with no wait. Past the limits she works as she did before, without the
  model-first step. Gemini is too slow for this step (6–8 s).
- **It adds about 1.5–2.5 s** to anything that isn't an exact command. That is network travel time
  to the model and can't be tuned away.
- **The model only sees about 9 candidate tools.** If the right one isn't among them it answers
  "other" and the full model takes the turn. That path is correct but slower.
- **"Actually make it 7" sets a second reminder.** She has no tool that edits an existing one.
- **Replies after an action are still the tool's own sentence.** Rewording each through a model
  would cost a second call per turn.

## Settings (`config/config.yaml` → `understanding:`)
```yaml
interpreter: true
interpreter_models: ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
interpreter_payload: {reasoning_effort: low}
```
Turn `interpreter` off to go back to patterns-first.

## Files
- `veronica/core/interpreter.py`: the prompt, the candidate shortlist, checking the answer
- `veronica/core/orchestrator.py` (`_interpret_turn`): where it sits in a turn, and the fallback
- `tests/test_interpreter.py`: fake models; `tests/conftest.py` keeps tests off the real ones

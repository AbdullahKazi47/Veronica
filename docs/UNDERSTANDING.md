# Understanding what you mean

Different ways of saying the same thing should reach the same action and give the same kind of
answer. "What's happening around the world" and "what's the news around the world" both give the news
briefing; "Chrome band karo" closes Chrome like "close Chrome" does.

## Why this was needed
Quick commands were matched by fixed word patterns (regexes). If you said it differently, she either
missed it and handed it to the cloud model (slower, uses quota, sometimes a different answer) or a pattern
grabbed the wrong tool. A real case from the log on 20 Oct: *"Okay, tell me what's happening around the
world today"* ran the **calendar**, because the calendar pattern took anything with "what's … today".

## How it works (`veronica/core/intents.py`)
1. **Exact patterns first.** The existing quick commands still run first and are unchanged.
2. **Meaning next.** Every capability has an intent with 6–35 example phrasings
   (`veronica/core/intent_catalogue.py`): formal, casual, indirect, questions, Hinglish in Latin script,
   and common mishearings. Your request and the examples are compared after a normaliser maps synonyms
   and Hindi words to one form ("band karo" → close, "kam karo" → lower, "duniya" → world,
   "kya chal raha hai" → what is happening, "too loud" → lower volume).
3. **Decide by confidence.**
   - *Clear match*: she runs the tool straight away, with no cloud call. Arguments come from the tool's own
     reader, the intent ("too loud" → volume down), or a slot finder ("take me to placement" →
     folder "placement"; "get rid of that Chrome window" → app "Chrome", only if such a window is open).
   - *Two actions equally likely*: she asks one short question, e.g. "Do you mean what's on your calendar, or
     the news?" or "What would you like me to close?".
   - *Medium*: the model decides, told the top 3 likely tools.
   - *Small talk*: the model, as before. No tool runs.
4. **A wrong pattern is overruled** when the meaning clearly points elsewhere. This fixes the calendar case
   above; the calendar pattern itself was also tightened.
5. **Learning from corrections.** "No, I meant X" already teaches her that exact phrase
   (`data/learned_fixes.json`). Now the wording she got wrong is also stored as a new example for the
   intended action (`data/intent_examples.json`), so similar phrasings land right next time.
   "When I say X I mean Y" keeps working through the existing aliases.

## Safety (unchanged)
Understanding only picks the intent. Every action still goes through `ToolRegistry.dispatch()` and the
PermissionGate. A risky (SENSITIVE/CRITICAL) action is never taken on meaning alone unless your words
name its target (an open window called "Chrome"). "Get rid of all of it" goes to the model, and the gate
still asks in full words. Lines that could mean two different actions get a question, never an action.

## Results
Paraphrase set: `tests/paraphrase_set.py`. It has 17 intents × 10–12 phrasings (172 lines: English,
casual, indirect, Hinglish, mis-transcribed), 20 small-talk lines and 10 ambiguous lines. It is kept
separate from the catalogue's examples: those teach, these measure. Run it with
`python scripts/measure_understanding.py` (add `-v` to see the misses).

| | before (patterns only) | after |
| --- | --- | --- |
| correct routing | **47%** (80/172) | **98%** (169/172) |
| small talk that triggered a tool | 1 ("good morning" → briefing, by design) | 1 (same) |
| ambiguous lines answered with a question | 0/10 | 9/10 (the 10th goes to the model) |
| risky actions from ambiguous lines | 0 | 0 |
| time added per request | — | ≈ 1.3 ms median, 6 ms max (matcher alone) |

The three misses are all safe. "Check what's wrong with my PC" goes to Fix-it instead of diagnostics.
"Read this for me" and "is the laptop charging" get a short question.

## Why no AI embedding model
Sentence-transformers is installed, but importing it (PyTorch) takes about 57 s on this PC. No
multilingual ONNX model is downloaded either. The built-in matcher (words, word pairs and character
4-grams, TF-IDF weighted) needs no model or download, runs in about a millisecond, and already reaches
98% on the set. A dense model could be added behind `IntentMatcher` later if needed.

## Adding phrasings
- A new tool can declare `intents = [{"name": ..., "examples": [...], "args": {...}, "slot": "name"}]`
  on its class, or get an entry in `intent_catalogue.py`.
- Wrong guesses are taught by voice: "No, I meant the news."
- Switch it off with `understanding.intents: false` in `config/config.yaml`.

## Try it
"Catch me up" · "Duniya mein kya chal raha hai" · "Shut the browser" · "Chrome band karo" ·
"It's too loud" · "Volume kam karo" · "What's my day like?" · "Take me to placement" ·
"Mausam kaisa hai" · "PC hang ho raha hai" · "Close everything" (she asks what).

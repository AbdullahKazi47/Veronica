# Hearing: the speech chain, measured

Everything VERONICA does starts with hearing you correctly. A misheard app name does not
degrade gracefully — "close Chrome" heard as "close Crow" finds no program and the command
simply fails. This phase measured the recognizers instead of trusting their reputations,
reordered them accordingly, and taught her to notice the specific moment she has probably
misheard.

## What was actually wrong

The shipped chain was **Groq → Google → offline**. Groq was chosen because it measured 8%
word errors against Google's 24% back on 2026-10-16. Then, for a day, every Groq request
from this network answered `404` — unauthenticated ones too, so it was not the key — and
the chain fell through to Google silently. The config said "most accurate first" while the
running assistant used the least accurate engine on every sentence, with no error anywhere.

Groq then started working again on its own (5.4 s, reachable as of 2026-10-17). That is the
real lesson: the problem was never Groq's accuracy, it was building the first rung out of
something that can vanish without telling you. A chain whose best case needs the internet
has no good case when the internet is the thing that broke.

## The measurements

Identical clips, Windows SAPI at 16 kHz mono, on this PC (Latitude 5520, CPU capped at
1.8 GHz). Real room audio is harder than synthesised speech, so these numbers are only
meaningful *against each other*:

| engine | word errors | time | cost |
| --- | --- | --- | --- |
| offline Whisper `base` | **2.9%** | 1.2 s | free, no internet, audio never leaves the PC |
| Gemini Flash-Lite | **1.3%** | 1.9 s | free daily quota (~20 req/day), audio goes to the cloud |
| Google Web Speech | 10.4% | 1.3 s | free, audio goes to the cloud |
| Groq whisper-v3-turbo | 8%, earlier clip set | 5.4 s | free quota, cloud, comes and goes |

Groq's 8% is not from the same clips as the rest, so read it as "somewhere between offline
and Google", not as a fourth comparable number.

Google does not merely misspell — it drops whole words. It heard *"Veronica, ask Jev whether
the bus from Bhatkal leaves at four"* as *"Veronica asked whether the bus from Badkal..."*,
losing both the verb and the tool name. A lost word cannot be recovered downstream; a
misspelled one often can.

## The chain now

**offline Whisper → Groq (only if a key is set) → Gemini → Google.**

Offline leads despite Gemini being marginally more accurate, because Gemini's advantage
(1.6 points) is worth less than three things offline gives for free: it works with no
internet, it spends no quota, and your voice stays on this machine. A per-sentence Gemini
call would exhaust the free tier before lunch. Google is last because of the dropped words.
Groq keeps its place as the first cloud rung, but nothing depends on it being there.

`backend: groq_whisper` still means Groq only, for running VERONICA somewhere Groq answers.

## The second opinion

Offline answers alone unless the read looks wrong, and then exactly one cloud re-read is
allowed. Two independent signals decide, because neither is sufficient:

1. **Low confidence** — Whisper's own `avg_logprob` below `escalate_below_logprob` (−0.55).
2. **An unfindable program** — the transcript gives an open/close verb a target that
   resolves to no app, browser or folder.

The second signal exists because the first does not catch the errors that matter. Whisper is
*confidently* wrong about names: "close Crow" scored −0.50 and "open NodePad" −0.28, both
above any bar that would not also escalate half of ordinary speech. What those reads have in
common is not doubt, it is naming a program that does not exist — so that is the trigger
(`names_an_unknown_app` in `veronica/tools/app_index.py`).

It stays quiet when the fuzzy app match already rescues the mistake: `find_app("NodePad")`
returns Notepad, so the command works and no request is spent. Compound targets are split —
"open Claude and Cursor" is two programs, not one oddly-named one. A discovery failure
returns "no escalation": escalation is an optimisation and must never be the reason audio
leaves the PC.

Measured end-to-end over ten spoken commands: **one** escalation (the Chrome one, corrected
to "close Chrome"), nine turns answered locally in ~0.9 s.

## The vocabulary hint, and the trap in it

Whisper takes an `initial_prompt` to prime rare words. Priming it with your installed app
names fixed app recognition — and then broke it, because the junk filter that rejects
hallucinated hint-echoes started convicting *correct* transcripts: "switch to Docker Desktop"
and "close Microsoft Teams" were both thrown out as "echoed the hint", and app word-error
rose from 27.5% to 46.2%.

So the hint now has two audiences, which is why there are two builders in
`veronica/speech/vocabulary.py`:

* `build_vocabulary` — what Whisper is *primed with*: your name for her, place names,
  aliases, and up to 55 installed app names.
* `build_echo_terms` — what the junk filter may *convict* on: only the rare personal words
  (VERONICA, Jev, Kokti, Bhatkal). An app name in a transcript is almost certainly a real
  command, not a hallucination.

Result: app word errors 21.2% with **zero** false rejections, short commands 0.0% with zero
false rejections — better than the shipped behaviour on every axis.

## Settings

In `config/config.yaml` under `stt:`

| key | default | meaning |
| --- | --- | --- |
| `offline_whisper_model` | `base` | the local model size |
| `gemini_stt_model` | `gemini-flash-lite-latest` | the second-opinion recognizer |
| `escalate_below_logprob` | `-0.55` | confidence bar for a re-read; `0` disables escalation entirely |
| `whisper_min_logprob` | `-0.78` | below this the read is discarded as junk |

Setting `escalate_below_logprob: 0` keeps every clip on this PC, at the cost of the
occasional misheard name.

## A bug worth remembering

Escalation shipped dead. The gate read:

```python
if engine is not self._recognize_local_whisper:   # always true
```

Python rebuilds a bound method on every attribute access, so `self.m is self.m` is `False`
and the condition never admitted anything. Every test passed, because the tests
`monkeypatch.setattr` the engine — which stores a plain function, making identity hold.

The fix is that `_engines()` now returns `(label, function)` pairs and the gate compares
labels. The regression test
(`test_escalation_fires_through_the_real_offline_method`) stubs the *decoder* and leaves the
real bound method in the chain, so only a label-based gate can pass it.

The general lesson, written down because it will recur: a mock that replaces the thing whose
identity is under test cannot test that identity. This was found by running the real chain
over real audio, not by reading the suite.

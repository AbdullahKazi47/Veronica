# Staying up: free quotas, dead rungs, and honest failure

VERONICA runs on free tiers. That is a deliberate constraint, not a temporary one — and it
means the interesting failure is never "the code is wrong", it is "the thing the code
depends on stopped answering today". This phase went looking for those, live, and found
three that no test could have reported.

## What was found by asking the services, not the config

**The reasoning model was out of quota.** `gemini-3.5-flash`, the model configured for
reasoning, analysis, planning and coding at priority 20, answered `HTTP 429` to text, tool
and streaming requests. Every reasoning turn was starting on a dead rung and falling
through. Gemini Flash's free allowance is roughly 20 requests a day.

**Each model has its own allowance, and it is small.** `gemini-3.6-flash` answered three
probe requests and was 429 by the fourth. This is the single most important fact about
running on the free tier: the ladder's *breadth* — how many distinct models it can reach —
matters far more than the order of the top two.

**Groq was not blocked after all.** Every Groq request from this network answered `404` for
a day, unauthenticated ones included, which looked like a regional block. It now answers
normally (chat in 2.6 s, Whisper in 5.4 s). A rung that disappears and returns on its own
is a rung you can use but never depend on.

## The model ladder now

Measured on this PC, text + tool call + streaming on each:

| model | latency | role |
| --- | --- | --- |
| `gemini-3.5-flash-lite` | 1.9 s | default, everyday turns |
| `gemini-3.6-flash` | 2.0–2.6 s | **leads reasoning** — newest and fastest |
| `gemini-3.5-flash` | 4–6 s | reasoning (was 429 when measured) |
| `gemini-3.7-flash` | 3.8–5.1 s | reasoning |
| `gemini-3.8-flash` | 6.0–10.6 s | reasoning, last Google rung |
| `gemini-3.1-flash-lite` | 3.6–9.0 s | fast |
| `openai/gpt-oss-20b`, `-120b` | — | Groq, when Gemini fails outright |

`gemini-3.6-flash` was promoted ahead of `3.5-flash` because it is both newer and ~2.5×
faster at the same quota cost. `3.8-flash` was added last rather than first: it works, but
at 6–10 s it is a rung worth having, not a rung worth leading with. Its `timeout_s` is 45,
because a short timeout on a genuinely slow model throws the rung away.

`max_fallback_attempts` went 6 → 8 to match the longer ladder; there is no point
configuring rungs the router will not reach.

Note that the newest *flash-lite* is still `3.5-flash-lite`, so the default model did not
change. `3.8` only publishes flash-lite in TTS form.

## Screen reading survives a spent quota

"What's on my screen?" asked two models and then gave up on the cloud. Two findings:

* **It retried a 429 after 1.5 s.** A 429 is the day's quota for that model; sleeping
  cannot refill it. With several models in the list that is seconds of pure waiting before
  each inevitable failure. Now only `5xx` — which really does mean "busy right now" — earns
  one retry, and a 429 moves to the next model immediately.
* **The list was too short.** `llm.vision_fallback_models` adds more image-capable models,
  each with its own allowance. All were verified to read text out of a real image:
  `3.5-flash-lite` 3.7 s, `3.1-flash-lite` 5.4 s, `3.7-flash` 16 s.

The offline vision model on this PC remains the last resort, so the image can still be
described with no internet at all.

## The live check now checks what ships

`scripts/feature_check.py` was asserting things that had quietly stopped being true:

* **Location** failed on the *correct* answer. It rejected any place name containing a
  comma as "a made-up village" — but "Kokti, Bhatkal" is the user's actual village and
  town, confirmed against a 109 m Windows fix. It now checks that there is a place and that
  she is honest about where the place came from.
* **Hearing** was tested through ElevenLabs (free quota spent, `401`) into Groq (404 that
  week) — two services VERONICA no longer uses by default. It now synthesises speech with
  Windows SAPI and runs the real offline recognizer: no network, no quota, and a genuine
  dependency break would actually show up.
* **The voice round trip** is now her configured Kokoro voice into her configured ears,
  entirely on this PC. It passes: `bf_emma` says "Minimise all my tabs", she hears
  "Minimize all my tabs."
* **Every module imports.** The unit suite only parses the source; it cannot see a
  dependency whose API moved. That is not hypothetical — PyAV 19 dropped a keyword that
  faster-whisper's file decoder passes.

### SKIP is not PASS

A third party refusing to serve us is not a VERONICA defect, but it must not read as
success either. `SkipCheck` reports **SKIP** with the provider's own status in the message,
and is raised only for an outage outside this machine — a wiring error or a malformed
response still fails loudly. The distinction matters: a check that turns green when a
service dies is worse than no check.

Current state: **97 passed, 0 failed, 1 skipped** (the deliberate screen/camera skip).

## Ctrl+C works again

Seventeen bare `except:` clauses had accumulated around Windows COM property reads in the
vision layer. The handling was right — individual property reads do fail per element — but
a bare `except` also catches `KeyboardInterrupt`, `SystemExit` and `MemoryError`, so a long
UI-tree scan could not be interrupted. All now `except Exception:`, with
`tests/test_source_hygiene.py` guarding against the habit returning.

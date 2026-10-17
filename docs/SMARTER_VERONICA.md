# Smarter VERONICA (2026-10-17)

`veronica/core/understanding.py`, `veronica/core/grounding.py`, `veronica/tools/understanding_tools.py`.
On with `understanding.enabled: true` in config.yaml (off by default in code, so tests and
other set-ups are unchanged).

| # | What | How to use it |
|---|---|---|
| 1 | **Checks her own work.** Every figure in an answer to a factual question is checked against the sources and what her tools returned that turn; one that isn't there gets "I couldn't confirm X in my sources, so double-check that." | automatic |
| 3 | **Grounded answers.** Factual questions ("when did…", "how tall…", "who is…") are looked up on Wikipedia first (official API); she answers from it, names the source, and says she isn't sure when it doesn't cover the question. "Look up X" reads the summary out. | automatic; "look up the Treaty of Versailles" |
| 4 | **Knows what's on screen.** When a request points at the screen ("this error", "this page", "close this"), the active window's title and program go with it, as data. "Close this" closes the window in front (still asks first). | "what does this error mean", "close this" |
| 5 | **Keeps the thread.** "Do that again", "close it", "use the second one" are filled in from what she just did or listed (kept 30 minutes, across restarts). | automatic |
| 6 | **Learns your words.** | "my notes means Obsidian", "by Ammi I mean my mother", "what words have you learned?", "forget what my notes means" |
| 7 | **Reads the moment.** One-line style note: in a hurry ("quickly"), wants detail ("why", "explain"), frustrated, late at night, or in a full-screen game. | automatic |
| 9 | **Multi-step tasks end to end.** "Research…/compare…/find the best… and save/tell me…" is one goal for the model with its tools (look_up, browser_task, take_note), run in the background in voice mode, announced when done. | "research the best budget phone, compare three and save a summary to my notes" |
| 10 | **Learns from corrections.** After "No, I meant Obsidian" the corrected request runs, and the same words do the right thing next time. | "what did you get wrong this week?", "forget that correction" |

Stored in `data/aliases.json`, `data/learned_fixes.json`, `data/conversation_state.json`.

Safety: rewritten requests go through the same tools and PermissionGate as anything said;
window titles and fetched pages are labelled as data, never instructions; general web
search pages that answer automated requests with bot checks are not used.

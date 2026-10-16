# Getting Started with VERONICA

## 1. Requirements

- Python 3.10+ (tested on 3.12)
- Windows, macOS, or Linux (the OS-specific tools degrade gracefully where a platform
  API isn't available — see `docs/ARCHITECTURE.md`)
- A microphone/speakers only if you want voice mode
- **No paid API key is required** for anything in the default configuration.

## 2. Install

```bash
cd veronica
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp config/.env.example config/.env
```

You don't need to edit `config/.env` at all to get started — it's only for optional paid
providers.

## 3. Run in text mode (works immediately, no setup)

```bash
python -m veronica.main
```

By default `llm.provider` is `ollama`, which needs a local model — see step 4. If you
just want to try the tool system first without any model at all, temporarily set
`llm.provider: echo` in `config/config.yaml`; every fast-path command (weather, time,
calculator, open app, etc.) still works, and anything that would otherwise go to the LLM
just echoes back so you can see the plumbing.

## 4. Set up the free local LLM (Ollama)

1. Install [Ollama](https://ollama.com/download) for your OS.
2. Pull a model (this one is small enough for most laptops):
   ```bash
   ollama pull llama3.1:8b
   ```
   If your machine is limited on RAM, try a smaller model like `ollama pull llama3.2:3b`
   and change `llm.model` in `config/config.yaml` to match.
3. Make sure the Ollama server is running (`ollama serve`, or it's already running as a
   background service after install).
4. Run VERONICA — `config.yaml`'s `llm.provider: ollama` is already the default.

## 5. Voice mode

```bash
python -m veronica.main --voice
```

**With the wake word** (listens only after you say "Veronica"):

```bash
python -m veronica.main --wake
```

Say "Veronica", wait for the short beep, then say your request. After VERONICA answers
she goes back to waiting for the wake word. Needs `openwakeword` and the trained model at
`voice.wake_model_path` (train it with `VERONICA_WAKE_TRAINING_COLAB.ipynb`; check it with
`python voice_check.py --skip-stt`). Tune sensitivity with `voice.wake_threshold`
(default 0.5; lower = wakes more easily). If the model or openwakeword is missing,
VERONICA says why and falls back to plain `--voice` behavior. The wake word grants no
extra permission — requests still go through the normal permission prompts.

**Voice-only everyday commands** (instant — handled without the AI model):

| Say | What happens |
|---|---|
| "my city is Mumbai" | remembered in `data/profile.json`; used for weather and the briefing |
| "how's the weather?" / "weather in Delhi tomorrow" | current weather (home city if none named) |
| "good morning" / "brief me" | spoken briefing: time, weather, today's reminders, to-dos, 3 headlines |
| "what's the news" / "news about cricket" | top 5 headlines (Google News feed, no key; `assistant.news_region`) |
| "remind me in 10 minutes to call mom" / "at 5 pm" / "tomorrow at 9 am" | reminder, announced out loud when due |
| "set a timer for 25 minutes" | timer |
| "what are my reminders" / "cancel the reminder to call mom" | list / cancel |
| "add milk to my to-do list" / "what's on my to-do list" / "mark milk as done" / "remove milk from my to-do list" | to-do list (`data/todos.json`) |

| "remember that my sister's birthday is 12 March" / "what do you remember about me" / "forget my sister's birthday" | long-term memory (`data/memory.json`); facts go with every question (`memory.include_facts_in_prompt`) |
| "forget everything" | deletes all remembered facts — always asks for confirmation |
| "open WhatsApp" / "open Downloads" | any installed Start-menu app, or a folder in your home |
| "open YouTube in Brave" / "go to netflix" / "search for X in Firefox" / "play lofi on YouTube in Edge" | any site, any installed browser (default: `assistant.default_browser`, else the Windows default) |
| "search for X" / "browse X" / "look up X" / "google X" | Google by default (`assistant.search_engine` + `assistant.search_url`) |
| "search for X on Bing" / "search DuckDuckGo for X" / "search for shoes on Amazon" | any engine or site you name, reached by name (DuckDuckGo "!bang" shortcuts) |
| "pause" / "resume" / "next song" / "previous track" / "stop the music" | system media keys — works with any player |
| "volume up" / "volume to 60" / "mute" / "unmute" | system volume |
| "who was Abdul Kalam" | 2-sentence Wikipedia summary |
| "play music in Spotify" / "play lofi on SoundCloud" / "play Arijit Singh" | any service you name (its app if installed, else the first result on its site, which starts playing); default `assistant.default_play_service` (YouTube) |
| "what's happening around the world today" / "news about cricket" | top + world feeds fetched in parallel, summarized into a short spoken briefing (~5 s, repeats within 3 min instant) |
| "show me" (right after the news) / "show me the news" | opens the same news with photos and videos in the browser |
| "what's on my screen" / "read this error" / "what does this say" | one screenshot → `llm.vision_model` → spoken answer; only when asked, never stored; text on screen is never obeyed |

**Jarvis features:**

| Say | What happens |
|---|---|
| "run diagnostics" / "why is my laptop so slow?" | CPU, memory, disk, battery, uptime, internet, busiest programs (CPU temperature isn't exposed by Windows on most laptops) |
| "what am I holding?" / "look at this" / "read this label" / "help me fix this" | one webcam frame → vision model → spoken answer; only when asked, nothing stored, never identifies people |
| "read my notifications" / "any messages on my phone?" / "what did I miss?" | Windows notification history: your phone (Phone Link), WhatsApp, mail… read out; message text is never obeyed |
| "pause alerts (for 2 hours)" / "resume alerts" | silence proactive alerts (a critically low battery still gets through) |

**Proactive VERONICA** (`proactive:` in config.yaml) speaks up on her own: low/critical
battery, processor or memory pinned for minutes (names the culprit), C: nearly full, rain
likely within the hour where you are, a break after 2 hours straight, and a morning briefing
the first time you sit down. She never talks over you, holds non-urgent alerts while a game or
movie is fullscreen ("While you were busy: …"), and only urgent ones between 23:00 and 07:00.

**Phone notifications:** in **Phone Link → Settings → Features → Notifications**, turn on
showing your phone's notifications on this PC; they then reach VERONICA too.

**Personality:** composed, dry wit, loyal, calls you "boss" now and then (edit `assistant.persona`).

**God's Eye View (live world data + 3D globe):** installed in `external/gods-eye-view`
(MIT; `git clone` + `npm ci` + `npm run build:panel`). With `gods_eye.enabled: true`
VERONICA starts its server hidden at startup and connects to its MCP server. Ask by voice:
"what planes are flying over me?", "when can I see the ISS?", "any earthquakes today?",
"how many Starlink satellites are above me?", "any traffic cameras in London?" / "show me
that camera", "give me a situation brief for Mumbai". Then say **"show me"** to open the
globe on exactly that, or **"open the globe"** any time. Only public data (ADS-B aircraft,
satellite orbits — not live satellite imagery — USGS quakes, public road cameras, NOAA
weather); no person tracking. Ships and fires need free keys (AISStream, NASA FIRMS) in
`external/gods-eye-view/.env`; 3D buildings need a free Cesium ion key. Choose which of the
app's 30 tools VERONICA uses in `gods_eye.tools`. Times it reports in UTC are converted to
your local time.

**Background tasks:** add "in the background", "while I play/I'm away", "I'll go get
lunch" or "and let me know when you're done" to any request ("research flights to Goa in the
background"). It runs in its own session while you keep talking; VERONICA announces the
result when it's done (also kept in `data/background_tasks.json`). Ask "what are you working
on?" / "what did you do while I was away?". A background task never asks for permission:
steps that would need your OK (writing/deleting files, shell commands) are skipped and
reported when it finishes.

**Conversation mode:** after VERONICA answers, keep talking for 8 seconds without saying
"Veronica" (`voice.follow_up_seconds`; 0 turns it off). Silence, "thanks", "that's all" or
"never mind" end the conversation and she waits for her wake word again.

Location: with `assistant.auto_detect_location: true` and no city set, VERONICA uses
**Windows location** (Wi-Fi based; your locality, weather for the exact coordinates, place
name from OpenStreetMap), falling back to your internet connection's city (IP → ipwho.is /
ipapi.co). Detected at startup in the background, re-checked every 6 hours, announced
once; "my city is X" always wins. Weather includes today's high/low and rain chance.

Nothing is hard-coded: apps come from the Start menu (`Get-StartApps`, cached in
`data/app_index.json`, refreshed when something isn't found), browsers from the Windows
registry, sites are resolved by name (`data/site_cache.json`). VERONICA also saves facts on
her own when you mention something lasting ("I'm vegetarian") and says so; she refuses to
store passwords, PINs, card/account numbers and keys. The recent conversation is restored
after a restart (`memory.persist_conversation`, `memory.resume_hours`).

**One-click start:** double-click **VERONICA** on the Desktop (or `Start VERONICA.bat`). It
starts minimized and listens for the wake word. At startup it prints where her voice goes,
e.g. `Voice output: Headphones (Realtek) at 18%` — if that's not where you're listening,
switch the output with the taskbar speaker icon. The microphone is chosen by name
(`stt.mic_device_name`), so Windows renumbering audio devices can't break it.

Reminders that came due while VERONICA was off are announced at the next start
("Missed reminder from 5:00 PM: …"). In voice mode, permission questions are spoken and
answered by voice: say **yes**/**no**; for CRITICAL actions (deleting files, shell
commands) you must say **confirm** — silence or anything unclear counts as no.

- **Speech-to-text** defaults to the free Google Web Speech backend via
  `SpeechRecognition` (needs internet, no API key). Its microphone needs **PyAudio**,
  which is *optional* — VERONICA's base install intentionally ships no mic package
  (PyAudio has no prebuilt wheel for every Python version, so requiring it would break
  `pip install -r requirements.txt` on some Pythons). For voice mode run
  `pip install PyAudio` (grab a prebuilt wheel for your Python if pip tries to compile
  it), or skip PyAudio entirely and use an offline backend below, which capture audio
  via `sounddevice`. If PyAudio is missing, voice input is simply unavailable until you
  add it — everything else keeps working. Offline options:
  - **Vosk**: download a [Vosk model](https://alphacephei.com/vosk/models) (the "small"
    English model is a good start), point `stt.vosk_model_path` at it, set
    `stt.backend: vosk` in `config.yaml`, and `pip install vosk sounddevice`.
  - **faster-whisper** (best offline quality, CPU or CUDA): `pip install
    faster-whisper`, then set `stt.backend: faster_whisper`. Models are downloaded
    automatically on first use (`stt.faster_whisper_model`: `tiny` → `base` → `small`
    → `medium` → `large-v3`); CTranslate2 falls back to CPU unless CUDA is available.
- **Text-to-speech** defaults to `edge-tts` (free, natural, needs internet, no key).
  Offline options:
  - `tts.backend: pyttsx3` — fully offline, robotic.
  - `tts.backend: piper` — fully offline neural voices (a big step up from pyttsx3):
    `pip install piper-tts`, then set `tts.piper_model` to a voice you've downloaded
    (e.g. `en_US-lessac-medium`). Uses the `piper` CLI if it's on your PATH, else the
    Python package, and caches rendered audio so repeats are instant.
- **Audio playback needs no extra package on Windows.** Every backend synthesizes a
  file (edge-tts → mp3, piper → wav) and plays it through the standard library:
  `winsound` for WAV and the winmm MCI driver (via `ctypes`) for MP3. pygame is no
  longer required for anything and is only an *optional* player for non-Windows
  platforms (`pip install pygame` if you're on macOS/Linux and want sound there).

## 5.5 Stitch GUI (the command-center front-end)

VERONICA ships a second front-end — the **Stitch GUI**, a cinematic glassmorphism command
center (Three.js particle core, live HUD, voice toggle) served over plain HTTP + SSE.
No PyQt, no paid key.

```bash
python - <<'PY'
from veronica.config import load_config
from veronica.main import build_app
from veronica.ui.stitch.bridge import run_bridge

config = load_config()
app = build_app(config, interactive=False)   # no terminal confirm prompts in the GUI
run_bridge(app.orchestrator, config, host="127.0.0.1", port=7171, open_browser=True)
PY
```

Then open <http://127.0.0.1:7171>. The console accepts text commands (same engine as the
CLI), the mic button runs STT → orchestrator → TTS, and the HUD shows live CPU/RAM/network.
If `psutil` isn't installed the HUD shows `--` for CPU/RAM rather than failing. See
`docs/ARCHITECTURE.md` § 5.1 for how the bridge works.

**Multi-step agentic tasks.** The **Plan & Run** button (the 🌳 tree icon, next to Send)
submits the input as a *planned task* instead of a single command: VERONICA decomposes the
goal into ordered steps and runs them with per-step retry and self-verification. The glass
progress card in the chat panel shows the goal, a step bar, per-step checkmarks, and a
cancel button, while `task_started`/`task_progress`/`task_completed`/`task_failed`/
`task_cancelled` events stream over SSE. A goal that doesn't actually need multiple steps
is reported as such and run as a normal command. This is the `automation/` layer — see
`docs/ARCHITECTURE.md` § 5.2.

## 5.6 Learning & Adaptation

VERONICA records what it does, what happened, and what you told it — so it can get better
at the things it does often. It's fully local, fully offline, and optional.

**How adaptive planning works.** When you send a multi-step goal through the **Plan & Run**
flow, VERONICA first checks its store of past experiences. Anything relevant to the current
goal (similar wording, shared tools, recent and successful, or a prior failure worth
avoiding) is condensed into concise *lessons* and handed to the planner as **advisory
context** — e.g. "workspace_search succeeded repeatedly for locating unknown files;
direct path lookup failed when the filename was unknown." The planner LLM still generates a
fresh plan for the current goal; it may use that evidence or ignore it when the situation
differs. VERONICA **does not replay previous plans** — experience is evidence, not a
script. Failures are surfaced as *cautionary* context, never as absolute prohibitions.

The full loop is:

```
Experience → Lesson → Relevance → Confidence → Planner context → New plan
   → Execution → Verification → New experience
```

Turning it off is a one-line change — set `learning.enabled: false` in
`config/config.yaml`. This disables both experience capture and adaptive planning, and
restores the planner's normal, experience-free behavior. Task execution is unaffected
either way.

**Controlled behavioral adaptation (Phase 4).** At plan time, VERONICA may also transform
*how* a plan executes — but only under strict, conservative conditions and never
unrestrictedly:

- The learning system **never executes tools**. All execution continues through the normal,
  permission-gated pipeline. Adaptation only ever changes which step dicts the executor is
  handed (and it copies the plan, preserving the original).
- **Confidence thresholds** (the *same* `ConfidenceTracker` values as planning): below 0.80
  nothing is adapted at all; 0.80–0.90 is *advisory only* (informs, never silently changes);
  ≥ 0.90 *and* at least 3 repeated independent successes is *strong* and may substitute one
  tool for another within the same tool group (e.g. plan says `find_file`, validated lessons
  say `workspace_search`). An explicit user correction is treated as strong evidence but is
  still gated.
- **Safety first**: `delete_file`, `run_shell_command`, `write_file`, credentials, payments,
  and system/install operations are never silently adapted (their tool names live in
  `policy.SAFETY_SENSITIVE_TOOLS`), and the existing permission/confirmation rules stay
  authoritative.
- **Explicit instructions and current context win** over learned preferences: if your goal
  names the tool to use, or a plan step already has a concrete path/argument, that's left
  alone.
- **Reversible & explainable**: the original plan is always preserved, every applied/rejected
  adaptation is recorded with reason/confidence/evidence, and a failed adaptation discards
  itself and executes the original plan. The Experience log records `original_plan`,
  `adaptations`, `adaptation_applied`, and `adaptation_success` for review.

Behavioral adaptation is configured under `learning:` — `adaptation_enabled` (default true),
`adaptation_min_confidence` (0.80), `adaptation_strong_confidence` (0.90), and
`adaptation_min_evidence` (3). It is fully covered by `learning.enabled: false` (the adapter
is then never created).

**Where the learning data lives** (all under the project `data/` folder by default):

**Where the learning data lives** (all under the project `data/` folder by default):

- `data/experiences.jsonl` — the experience log: one JSON object per line, one per
  finished multi-step task (what was asked, which tools ran, outcome, retries, duration,
  failure info, feedback).
- `data/confidence.json` — per-strategy success/failure counts used to compute an
  evidence-based confidence score.

Both are configured under the `learning:` block in `config/config.yaml`. Set
`learning.enabled: false` to turn learning off entirely — task execution is unaffected.

**How to inspect or reset learning data.** Each line in `experiences.jsonl` is a complete
JSON record you can read with any JSON tool. To reset:

```bash
# Delete the experience log and confidence counts (fresh start):
del data/experiences.jsonl data/confidence.json
```

(Or from Python: `from veronica.learning import ExperienceStore; ExperienceStore("data/experiences.jsonl").reset()`.)

**Privacy / redaction.** Before anything is written to disk, secrets are stripped:
API keys, auth tokens, passwords, cookies, private keys, bearer tokens, JWTs, and
env-style secret names (`*_KEY`, `*_TOKEN`, `*_SECRET`, …) are replaced with
`[REDACTED]`. VERONICA stores *what tools ran and how it went*, not your credentials.
Redaction never changes what's in memory — only what gets persisted.

Learning can never run a tool, bypass the permission gate, or lower a confirmation
requirement; and if learning itself fails, VERONICA keeps working normally. See
`docs/ARCHITECTURE.md` § 5.3 for the full design.

## 5.7 Computer Perception & Control (Phase 7, core layer)

Phase 7 lets VERONICA perceive the Windows desktop and act on it through the same
permission-gated pipeline as everything else. This is the **core layer** — perception +
control + Phase 6 integration; continuous background observation and the vision-model
provider are later steps.

**What it does.**

- **Hybrid perception** (`veronica/vision/`): a `PerceptionLayer` queries providers in
  priority order — Windows UI Automation (semantic windows/controls/text), screen metadata,
  and OCR — and merges them into a single normalized `ScreenState` (serializable, with a
  SHA-256 fingerprint for change detection). UIA auto-disables on non-Windows or missing
  COM; OCR auto-disables when pytesseract is absent, exactly like the existing
  `ScreenshotTool` degrades.
- **Screen-change detection** (`change_detection.py`): configurable strategies
  (`idle` / `active` / `waiting` / `action` / `error`) with per-strategy thresholds, plus a
  `wait_for_change()` helper for observation loops.
- **Semantic-first control** (`computer_controller.py`): `find_and_click("Save")` resolves
  the element by name/type from the live `ScreenState`; direct coordinates are the fallback.
  Mouse moves and scrolls are SAFE; clicks, typing, and hotkeys are MODERATE (or SENSITIVE
  for destructive combos like `alt+f4`) — so they are confirmed by the same
  `PermissionGate` as any tool.
- **Computer-use loop** (`computer_use.py`): OBSERVE → UNDERSTAND → SELECT → PERMISSION →
  EXECUTE → OBSERVE → VERIFY. Execution failures surface `recovery_suggested` and feed the
  **existing** Phase 6 `ClosedLoopController` — there is no second recovery system.
- **Prompt-injection defense** (`screen_content_defense.py`): screen text is **untrusted
  data**. Forged-command patterns, permission-bypass phrasing, and credential-shaped text
  are blocked or filtered before screen content can influence a decision; `filter_perception_state()`
  produces the safe copy used by the loop.
- **Stitch GUI integration**: `requestScreenState`, `executeComputerAction`, and
  `cancelComputerAction` bridge methods plus `action_observing` / `action_executing` /
  `action_verifying` / `action_completed` SSE events.

**Dependencies.** All optional: `comtypes` (UIA), `pyautogui` (control — already in
`requirements.txt`), `Pillow` + `pytesseract` (OCR). On a machine without them the modules
import cleanly and report the providers as unavailable; nothing crashes.

**Safety is unchanged.** Every computer action is a permission-gated decision; screen
content can never override permissions, instructions, or the Phase 6 invariants
(`UNKNOWN` never becomes `SUCCESS`, stored experience is data, no "try it and assume it
worked").

## 6. Permissions — VERONICA asks before doing anything

By default (`safety.require_confirmation_above: SAFE`) **every single action is
confirmed in the terminal first**, including read-only ones like checking the time:

```
[VERONICA needs SAFE permission] get_current_time({})
Proceed? [y/N]

[VERONICA needs CRITICAL permission] run_shell_command({'command': 'pip install requests'})
Proceed? [y/N]
```

This is what makes it safe to give VERONICA a genuinely general-purpose "brain" — see
`tools/execution_tools.py`, which adds:

- `run_shell_command` — run any shell command on your machine
- `read_file` / `write_file` / `delete_file` — read, create, or remove any file
- `fetch_webpage` (in `web_tools.py`) — actually read a page's content, not just open it

Combined with the LLM's tool-calling, this means VERONICA can chain steps to accomplish
open-ended tasks ("read my notes.txt, summarize it, and save the summary as
summary.txt") — but it will stop and ask before every single step along the way. Nothing
runs, is written, or is deleted without an explicit "yes" from you first.

`run_shell_command` and `delete_file` are additionally `CRITICAL` permission (the
highest level) regardless of the threshold you set, and `run_shell_command` refuses a
small set of catastrophic patterns (`rm -rf /`, disk formatting, fork bombs, etc.) even
after you approve it — a second layer, not a substitute for confirmation.

Once you've gotten a feel for the assistant and trust the commands you use daily, loosen
`safety.require_confirmation_above` to `MODERATE` or `SENSITIVE` in `config.yaml` so
routine reads stop interrupting you — the execution tools stay locked at
`SENSITIVE`/`CRITICAL` either way. Set `safety.dry_run: true` (or pass `--dry-run`) to
have VERONICA describe what it *would* do without doing it.

### Everyday PC control (`tools/window_tools.py`)

This build ships with `require_confirmation_above: SENSITIVE`, so only destructive actions
ask. Everyday tasks have dedicated tools, so they never fall back to a shell command:

| Say | Tool | Asks? |
|---|---|---|
| "minimize all my tabs", "show desktop", "restore all windows" | `window_control` | no |
| "minimize Chrome", "maximize Spotify", "switch to WhatsApp" | `window_control` | no |
| "open the projects folder", "open veronica folder in projects", "open my resume pdf" | `open_path` | no |
| "lock my PC", "put the laptop to sleep" | `lock_or_sleep` | no |
| "cancel shutdown" | `cancel_shutdown` | no |
| "close Spotify" (unsaved work) | `close_window` | yes / no |
| "shut down my PC", "restart in 5 minutes", "sign out" | `shutdown_pc` | yes / no, then a 30 s cancellable delay |
| deleting files, shell commands | `delete_file`, `run_shell_command` | say "confirm" |

`open_path` searches your home folder by name (the shallowest match wins, and AppData and
hidden folders are skipped). It never runs programs or scripts (`.exe`, `.bat`, `.ps1`,
`.lnk` and others): apps are opened by name with "open <app>". A bare "go to sleep" does not
sleep the PC, because you have to name the PC.

## 7. Running the tests

```bash
pip install pytest
pytest tests/ -v
```

The suite covers everything that doesn't need a microphone, speakers, or a running
Ollama server: the permission gate, tool registry, memory (short-term, long-term, thread,
and conversation compression), the loop guard, compound-command splitting, LLM health and
circuit breaker, the capability layer, the Domino chain engine, the **multi-step task
executor and planner** (create/execute/retry/cancel/verify plus the orchestrator's agentic
path), the **Phase 6 closed-loop stack** (observation, diagnosis, failure memory, replanning,
the `ClosedLoopController`, orchestrator + Stitch-bridge integration, and adversarial edge
cases), the STT/TTS backends (stubbed, so no audio or model downloads are needed), the
Stitch bridge — including a real HTTP round-trip on an ephemeral port and live task
commands — the **Phase 7 perception & control core** (ScreenState serialization,
fingerprint/change detection, perception providers, computer-controller permission
enforcement, computer-use loop phases, prompt-injection defense, and safety invariants),
and the **Phase 8 Thread Memory V1** stack (data model, episodic projection, vector-store +
embedding abstractions, deterministic weighted retrieval with the relevance gate,
consolidation, import/export, redaction/defense-in-depth, the "no backend import" layering
rule, and orchestrator memory-context integration that must never grant permissions),
and the **Phase 9 Multi-Model Routing V1** stack (`ModelProfile`, task-type
classification, registry, cached health, ranking rules, capability-preserving fallback
with no fabricated success, provider construction, thread-memory isolation — memory can
never name a model — and orchestrator integration where routing never bypasses
permissions), and the **Episodic Memory Integration V1** stack (episode building from the
executor's authoritative Experience, conservative status derivation with `UNKNOWN` never
promoting to `SUCCESS`, episode record/retrieval through Thread, procedure evidence with
the OBSERVED → PROMISING → VALIDATED → DEGRADED progression, orchestrator + Stitch-bridge
integration, redaction before persistence, and the memory-write-failure isolation that
never fails a task). **626 tests pass (+1 skipped).** It's a good sanity check after adding a new
tool.

### Enabling Thread Memory

Thread Memory is opt-in and off by default. In `config/config.yaml`:

```yaml
memory_thread:
  enabled: true
  records_path: data/thread_memory.jsonl
  vector_path: data/thread_vectors.jsonl
  vector_backend: local        # "local" (zero-dependency) — ChromaDB/FAISS swap in later
  embedding_provider: local
  max_context_memories: 5      # how many retrieved memories are injected per turn
  retrieval_threshold: 0.20
```

With it enabled, VERONICA persists explicitly-remembered facts and completed episodes
(sanitized/redacted), and injects a small labeled `RETRIEVED MEMORY` block into the prompt.
It is purely informational: it never grants permission to act. Each completed task is also
projected into an **episode** (see `docs/EPISODIC_MEMORY.md`) that is retrievable as
`[RETRIEVED EXPERIENCE]` context and feeds procedure evidence — all advisory, never
authorization. See `docs/THREAD_MEMORY.md`.

### Enabling Multi-Model Routing (Phase 9)

Multi-Model Routing is **opt-in and off by default**; the single-model `llm.*` path is
unchanged when it is disabled. Define the models you want routed — each is an entry in
`config/config.yaml`:

```yaml
routing:
  enabled: true
  max_fallback_attempts: 2
  health_ttl_s: 30
  models:
    - model_id: "llama3.1:8b"        # default (fast/general)
      provider: "ollama"
      role: "fast"
      default: true
    - model_id: "qwen2.5-coder:7b"   # heavy reasoning / code
      provider: "ollama"
      role: "coding"
      task_types: ["coding", "reasoning"]
    - model_id: "gemma2:9b"          # reasoning (non-coding)
      provider: "ollama"
      role: "reasoning"
```

Useful per-model fields: `role` (`general | fast | reasoning | coding | vision`),
`task_types`, `capabilities`, `supports_tools`, `supports_vision`,
`supports_streaming`, `context_window`, `priority`, `speed_class` (`fast | medium | slow`),
`reasoning_class`, `cost_class` (`local | free | low | medium | high`), `temperature`,
`timeout_s`, `base_url`, `api_key_env`, `enabled`, `default`. Omit `routing.models`
entirely and routing registers your global `llm.*` model as the single routed default
(declaring tool support, so behavior matches the single-model path 1:1). The router's
selection order is: explicit instruction → required capability (vision/tools) → task type
→ context fit → availability → priority → latency → cost → default.

Routing is safe by construction: it only chooses which configured model answers — it
never grants tool permission (`PermissionGate` still governs every action), it never
probes a server in the middle of a request (health is TTL-cached), and if every routed
model fails the reply is an honest `ok=False` failure, never a fabricated success. A bad
routing config (unknown provider, duplicate model, no default) is rejected at boot and
VERONICA falls back to the single-model path. See `docs/MODEL_ROUTING.md`.

### Enabling Streaming LLM (Phase 21)

Streaming delivers the pure-LLM turn progressively and is **opt-in and off by default**
— an untouched config keeps the classic `chat()` path byte-for-byte. Turn it on in
`config/config.yaml`:

```yaml
streaming:
  enabled: true          # master switch
  preferred: false       # stream every supported turn (False = only when user says "stream it")
  max_response_chars: 8192   # accumulator cap (0 = uncapped); explicit truncated=True at the cap
  max_buffer_chars: 1024     # emit-buffer cap between delta events (never changes final text)
  coalesce_ms: 60            # minimum interval between delta events (0 = per-chunk)
  first_token_timeout_s: 30.0   # nothing received within this -> FAILED
  idle_timeout_s: 60.0          # silence between deltas -> FAILED
  overall_timeout_s: 300.0      # hard ceiling on one streaming turn
```

Behaviour once enabled:

- **When it streams**: the turn streams only when the selected model advertises
  `supports_streaming` (routed) or the provider advertises it (single-model path).
  The provider is resolved once, *before* any text flows — never a mid-stream switch.
- **When it doesn't**: no streaming-capable model → the turn honestly falls back to the
  classic `chat()` path and says so; a user who explicitly asked to stream but no model
  supports it gets an honest decline, never a fake stream.
- **Output**: coalesced SSE events (`stream_started`, `response_delta`,
  `stream_status`, `stream_completed`/`stream_cancelled`/`stream_error`) drive the Stitch
  GUI; the final accumulated text is what enters conversation and episodic memory.
- **Safety**: a cancellation or a stream failure never records partial output as an
  assistant reply; `cancel_stream()`/the GUI's cancel stops the active turn, and calling
  cancel when nothing is streaming is a harmless no-op. See `docs/STREAMING_LLM.md`.

### Enabling Context Window Management (Phase 21)

VERONICA's dedicated context layer decides **what goes into each request** — a structured
selection with per-source token budgets and status-preserving compression. It is **opt-in
and off by default**; an untouched config keeps the legacy message assembly
byte-for-byte. Turn it on in `config/config.yaml`:

```yaml
context:
  enabled: true                 # master switch
  default_context_window: 8192  # used when the routed model declares no window
  output_reserve: 1024          # tokens reserved for the model's reply
  safety_reserve: 256           # tokens kept free for the safety block
  # per-source budgets you may tune (defaults shown):
  budget_system: 2000
  budget_conversation: 3000
  budget_memory: 1200
  budget_tools: 1500
  budget_observation: 800
  compression_enabled: true     # drop lowest-value first, then summarize (status-preserving)
  inline_tools_in_prompt: false # true = tool schemas text-joined; default: budget-only
```

Behaviour once enabled:

- The current user request and the safety boundary are **always** included and **never**
  compressed. Everything else is scored for relevance and kept within its source budget;
  anything that cannot fit is dropped **for that request only** (stored memory is never
  touched) or summarized — and compression refuses to elide UNKNOWN/FAILED/BLOCKED.
- Tool schemas are budgeted but (by default) still handed to the model through its own
  `tools=` kwarg — the prompt stays clean of schema noise.
- The model's `context_window` is budgeted against as plain data from the routing layer;
  with routing off, `default_context_window` is used.
- **Safety**: any failure inside the context layer (including required context exceeding
  the model window) falls back to the legacy assembly with the user's full request
  intact — context management can never silently truncate your request. See
  `docs/CONTEXT_WINDOW.md`.

## 8. Adding your own tool

1. Create a class in `veronica/tools/` (or a new file) subclassing `tools.base.Tool`.
2. Set `name`, `description`, `permission`, and `parameters` (JSON-schema-style).
3. Implement `run(self, **kwargs) -> ToolResult`.
4. Optionally implement `matches()`/`extract_arguments()` so simple phrasings of the
   command skip the LLM entirely (the "fast path").
5. Register an instance of it in `tools/registry.py`'s `build_default_registry()`.

That's it — the orchestrator, the LLM's tool-calling prompt, and the permission system
all pick it up automatically.

## 9. Known limits of this build

- Voice and hardware-dependent paths (microphone, speakers, Windows volume control via
  `pycaw`, OCR via Tesseract) were built with clean interfaces and sensible fallbacks but
  could only be exercised with mocks/unit tests in the environment this was built in,
  which has no audio hardware and restricted network egress. Test them for real on your
  own machine — if something in a specific tool needs tweaking for your OS, that tool's
  file is short and self-contained, so it should be a small fix.
- `web_search` currently opens a DuckDuckGo results page in your browser rather than
  returning parsed results to the LLM — wiring up a real search API (with parsed results
  fed back into the conversation) is a natural next step; `config/.env.example` already
  reserves a `SERPAPI_API_KEY` slot for it.
- A GUI ships as a second front-end — the **Stitch command center** (§ 5.5), served over
  plain HTTP + SSE by `ui/stitch/bridge.py`. It talks to the same
  `Orchestrator.handle()` as the CLI, so the two front-ends never diverge in behavior.
  The browser page is static HTML + CDN JS (Three.js, Tailwind); it needs internet on the
  machine where you open it to load those assets.

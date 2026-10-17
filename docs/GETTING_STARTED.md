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
| "what's happening around the world today" / "news about cricket" | India's top stories first, then world headlines (international edition), fetched in parallel and summarized into a short spoken briefing (~5 s, repeats within 3 min instant) |
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
| "lock my PC" | `lock_pc` | no |
| "put the laptop to sleep" (the PC must be named; "go to sleep" only ends the conversation) | `sleep_pc` | yes / no |
| "cancel shutdown" | `cancel_shutdown` | no |
| "close Spotify" (unsaved work) | `close_window` | yes / no |
| "shut down my PC", "restart in 5 minutes", "sign out" | `shutdown_pc` | yes / no, then a 30 s cancellable delay |
| deleting files, shell commands | `delete_file`, `run_shell_command` | say "confirm" |

`open_path` searches your home folder by name (the shallowest match wins, and AppData and
hidden folders are skipped). It never runs programs or scripts (`.exe`, `.bat`, `.ps1`,
`.lnk` and others): apps are opened by name with "open <app>". A bare "go to sleep" does not
sleep the PC, because you have to name the PC.

### Learning and coding (`tools/learning_tools.py`, `memory/knowledge.py`)

VERONICA can study a subject and write code for you. What she learns is **memory**, not
model training: the model already knows Python, and the notes add fresh docs, your
preferences and lessons from her own work.

| Say | What happens | Asks? |
|---|---|---|
| "learn Python", "study FastAPI from https://…", "read up on Docker" | Reads Wikipedia (and the page you name), writes 12–25 study notes into `data/knowledge.json` | no |
| "what have you learned?", "what have you learned about Python?" | Lists topics, or reads highlights | no |
| "forget what you learned about Python" | Deletes that topic's notes | no |
| "write me a Python script that sorts my downloads by type" | Writes a project into `Documents\VERONICA Projects\<name>` and opens it in VS Code (or Notepad) | no, because nothing runs |
| "make it also handle PDFs" | Changes the last project; the old version is kept in `.versions\` | no |
| "run it" | Runs the project. If it fails, she fixes the code and reruns, at most `coding.max_fix_attempts` (2) times, then saves a lesson from the fix | **yes/no** |

- Notes and lessons are added to a request only when it names the topic ("write some
  Python…"), labelled as reference material. Web pages and program output are untrusted
  data and never instructions.
- Coding preferences you state ("I prefer pytest") are saved with `remember_fact` and used
  whenever she writes code.
- Code is written by the stronger routing model (role `reasoning`/`coding`). If it's busy,
  the main model is used.
- Her projects never contain `.exe`, `.bat`, `.ps1` or similar files, never write outside
  their folder, and never overwrite an earlier project. Programs run without input prompts
  and stop after `coding.run_timeout_s`.
- Settings are in the `coding:` section of `config.yaml`. `enabled: false` turns the whole
  feature off.

### Self-heal (`core/self_heal.py`, `core/jsonsafe.py`)

VERONICA watches her own health on a background thread and fixes what she safely can,
on her own:

- a stopped worker thread (the proactive monitor, a background-task worker) is **restarted**
- a corrupt memory/knowledge file is **restored from its backup** — every save keeps a
  `.bak`, and a damaged file is set aside as `.corrupt` rather than lost

She only **reports** (can't safely auto-fix) the language model being unreachable or the
disk being critically full. Say **"are you okay"**, **"run a self check"** or **"heal
yourself"** to run it on demand; it needs no permission because restarting and restoring
are non-destructive.

Hard limit, by design: self-heal **never edits code, changes settings, or touches the
permission gate.** It restarts and restores; it does not rewrite VERONICA. That boundary
is what keeps self-modification safe.

### Privacy guard — what reaches the cloud (`safety/privacy.py`)

VERONICA thinks with Google's Gemini in the cloud, so the honest truth is that **your
requests, and the facts you asked her to remember, do go to Gemini** — that is how she
answers you. There is no way around that while using a cloud model. What the privacy guard
guarantees is that a narrow, high-risk set **never leaves the machine**, even if you say
one aloud, it's on your screen, or it's in a file she reads:

- passwords, API keys, tokens, JWTs, bearer/authorization headers, private keys
- card numbers (validated by the Luhn check, so random long numbers aren't touched),
  CVV/PIN, bank account / routing / IFSC numbers
- government IDs: US SSN, India Aadhaar and PAN
- anything matching a regex you add in `privacy.extra_patterns`

Each is replaced with `[REDACTED]` at the single point every request passes through before
the network call — so it covers your spoken request, tool results, screen-question text and
replayed tool turns, not just one path. The guard is **on by default and fails safe**
(even an unconfigured guard redacts). It records only *how many* of each category it removed,
never the values, so **"have you redacted anything?"** can be answered without keeping the
very data it just protected. Ask **"what do you send to the cloud?"** or **"is my data
safe?"** for the full picture.

Your API key itself is never sent in a request body — it only ever goes in the
`Authorization` header to the provider you configured, and it lives only in `config/.env`
(gitignored). Settings are in the `privacy:` block of `config.yaml`.

Deliberate boundary: the guard sanitizes outbound text. It does not weaken the permission
gate, and the screen/camera image path still runs only when you explicitly ask her to look.

### Conversation mode — no wake word between turns (`voice.conversation_mode`)

With `conversation_mode: true` (the default), you say "Veronica" **once** to start, then keep
talking freely — no wake word between turns. The conversation stays open until you end it
("thanks", "thank you", "that's all", "bye", "stop", "never mind"). The only automatic exit
is `conversation_idle_timeout_s` (default 300 s): if you go silent that long, she drops back
to the wake word so the mic isn't live forever. Set it to `0` to never time out. Set
`conversation_mode: false` to use the old short `follow_up_seconds` window instead.

### Faster, interruptible, accurate voice (`voice/mic_hub.py`, `voice/barge_in.py`)

**Faster replies**
- One always-open microphone stream (`voice.shared_mic`) serves the wake word, speech
  recognition and the talk-over listener. Each listen used to reopen the device and throw
  away 0.5 s of warm-up silence, which also clipped words said right after the beep.
- Replies are spoken **sentence by sentence**: the voice starts after the first sentence is
  synthesized, and the next is prepared while the current one plays. ElevenLabs gets the
  neighbouring sentences, so intonation stays natural.
- The silence that ends your sentence is 0.65 s (`stt.pause_threshold`, was 0.8).

**Talk over her** (`voice.barge_in: wake_word`)
- Say **"Veronica"** while she's talking and she stops mid-word and listens, with no wake
  word needed for what you say next. She uses the wake-word model rather than "any loud
  sound" because laptop speakers feed her own voice into the mic. During a sentence that
  contains her own name, the listener stands down.

**Fewer mishearings**
- Google's alternative readings are checked. If the top reading isn't a command but an
  alternative is ("minimise all attacks" vs "minimise all tabs"), the command wins.
- Teach her: **"when I say attacks, I mean tabs"**, **"you heard crystal but I said
  restore"**. "What corrections do you have?" lists them, and "forget the correction for X"
  removes one. They're stored in `data/stt_corrections.json`.
- After the wake word, if Google reports low confidence (below
  `stt.confirm_below_confidence`), she asks "did you say …?". Google's free service only
  gives a confidence score for unclear audio, so clear speech is never questioned.

**Ignoring background noise**
- The room's noise level is tracked continuously, leaving out her voice and yours, so a TV
  switched on raises the bar for what counts as you speaking.
- Hum below 100 Hz (fans, AC) is filtered out before recognition (`stt.highpass_hz`), and
  bursts shorter than 0.25 s (clicks, coughs) are ignored.
- In an open conversation (no wake word that turn), she ignores speech that isn't at least
  3× louder than the room (`voice.conversation_speech_ratio`) or that she's unsure of
  (`voice.conversation_min_confidence`). Commands and ending phrases always get through.

### Jev, the decision layer (`core/jev.py`)

Jev is a fast, structured way of deciding that sits beside VERONICA's conversational brain.
It doesn't replace her; it only advises and never acts.

| Say | What happens |
|---|---|
| "Jev, should I take the metro or an auto?" / "help me choose between a Pixel and an iPhone" / "compare Python and JavaScript for a beginner" | A recommendation, the score, the main reason, the main risk, and what would change the answer |
| "Jev, why?" / "why did you pick that?" | The criteria, their weights and every score |
| "Jev, how would you handle 'minimise all'?" | Which quick command or which model would handle it, and the backups (without running it) |

How it works: the stronger model (Gemini Flash, or Groq if Gemini is down) lists the
options, the criteria that matter with weights from 1 to 5, and a 1–10 score for each option
on each criterion. **The ranking is computed in code as a weighted average**, so the
recommendation always follows from the scores and the model can't simply announce a winner.
Results within 0.5 are reported as a close call. Your remembered facts (city, preferences)
inform the scores. The last 50 decisions are kept in `data/decisions.json`. Speech
recognition often hears "Jev" as "Jeff" or "Chev", and those work too.

### Adding a new ability by voice (`core/abilities.py`, `plugin_api.py`)

Say **"add an ability for flight tracking"** (or "build a skill that tells me cricket scores").
1. **Build.** The coding model writes one small add-on.
2. **Safety check, before any of it runs.** Only plain-data imports and VERONICA's ability
   toolkit are allowed. There's no file or OS access, no `eval`/`getattr`, no names starting
   with `_`, and no `str.format`, which closes the known ways out of a Python sandbox.
3. **Test.** Its examples run in a separate Python process with a time limit. If they fail,
   she fixes the code and tries again, at most twice.
4. **Report.** She tells you what it does, which websites it uses and how the tests went.
5. **Approve.** "Activate it" asks yes/no. Nothing goes live without that.

What an added ability **can** do: look things up on the internet (web GET requests only) and
give you an answer. What it **can't** do: control your PC, read or write files, run
programs, reach your local network or localhost, or use VERONICA's own API keys. It can
only contact the websites it declared, and that is checked on every request, including
redirects. Approved code is fingerprinted (SHA-256), and an ability whose file has changed
since you approved it is refused at startup. "What abilities have you added?" lists them;
"remove the flight tracking ability" asks, then sets the code aside in
`data/abilities/removed/` rather than deleting it.

Tested live: "flight tracking" was built in ~40 s (OpenStreetMap to find the city, OpenSky for
the aircraft) and answered "There are 122 planes flying over London right now…".

### Calendar (`integrations/calendar.py`)

| Say | What happens |
|---|---|
| "what's on today?" / "what's on my calendar tomorrow?" / "what do I have this week?" / "anything on Friday?" | Reads the events |
| "what's my next meeting?" | The next event |
| "am I free tomorrow afternoon?" | Checks that time span (morning 6–12, afternoon 12–17, evening 17–22) |
| "add dentist to my calendar tomorrow at 4 pm" / "schedule a call with Ravi on Friday at 3" | Adds it to VERONICA's own calendar, with a reminder 10 minutes before |
| "remove dentist from my calendar" | Removes an event she added |
| "connect my google calendar" | Explains how to set it up |

The greeting when you open the laptop includes the rest of today's events.

**Connecting Google Calendar (read-only):**
1. Open Google Calendar settings, pick the calendar, and go to **Integrate calendar**.
2. Copy the **Secret address in iCal format**.
3. Add it to `config/.env` as `CALENDAR_ICS_URLS=<address>`, using commas to separate several
   calendars.
4. Restart VERONICA.

That address works like a password for reading the calendar, so keep it only in `.env`.
VERONICA reads repeating events, time zones, all-day events, skipped and moved occurrences,
and leaves out cancelled events. She can't change your Google events (that would need a
Google sign-in app). Events you add by voice are kept in `data/calendar.json`.

### Voice self-test

Say **"run a voice test"** (or "test your microphone") for a one-minute check:
1. She measures the room's noise while you stay quiet.
2. You say a test sentence. She reports what she heard, how sure she was, and how much
   louder you were than the room. If you were close to the "ignore background speech" bar
   (`voice.conversation_speech_ratio`), she offers to lower it, and only changes it if you
   say yes.
3. She reads a long sentence while you say "Veronica". She reports whether talk-over stopped
   her, and how quickly.

### Routines: one phrase, several commands (`core/routines.py`)

| Say | What happens |
|---|---|
| "create a routine called good night: set an alarm for 7 am, then lock my PC" | Saves it (steps split on commas, "then", "and then") |
| "good night" / "run work mode" / "start my leaving routine" | Runs each step in order and tells you what happened |
| "what routines do I have?" / "what does good night do?" | Lists them / reads the steps |
| "add mute the volume to my good night routine" / "remove step 2 from good night" | Edits it |
| "delete the work mode routine" | Removes it |

Each step is an ordinary spoken command and goes through the normal command path, so
**every step still asks permission where it normally would**. A routine with "put the
laptop to sleep" still asks yes/no. A routine can't start another routine. If a routine is
named like an ending phrase ("good night"), saying it runs the routine. Stored in
`data/routines.json`.

### Backup brain: Groq (`routing.models` role `backup`, `llm.backup`)

When Google's Gemini fails (its "503 overloaded" errors, or no connection), Groq answers
instead. The key goes in `config/.env` as `GROQ_API_KEY`.

- The routing chain is Gemini Flash-Lite → **Groq `openai/gpt-oss-20b`** → **Groq
  `openai/gpt-oss-120b`** → Gemini Flash. A different service is tried before the second
  Gemini model, because both Gemini models are usually down together.
- After a Gemini failure, the next ~45 s of turns go **straight to Groq**, so each turn
  doesn't wait on a request that's about to fail. Then Gemini gets another chance.
  (Before, a model that failed twice was skipped until VERONICA restarted.)
- Reasoning and coding tasks try the stronger 120b backup first.
- Direct model calls (news summaries, studying, writing code) fall back to Groq too
  (`llm.backup`).
- Measured on this PC: Groq 20b answers in ~1.4 s, 120b in ~3.5 s, and both call
  VERONICA's tools correctly.
- Groq sees the same requests Gemini would, and the privacy guard scrubs them the same way.

### When Google's AI is overloaded ("503")

A **503 Service Unavailable** from `generativelanguage.googleapis.com` means Google's
Gemini servers were overloaded. Nothing is wrong with VERONICA, your key or your PC.
VERONICA now:
- retries twice (after ~0.7 s and ~1.8 s), which usually gets through;
- if Google is still down, says so plainly ("Google's AI service is overloaded right now…")
  instead of reading out an error with a web address in it;
- if a tool already ran that turn (a folder opened, say), tells you that too, so an outage
  can never hide something she did;
- handles casual phrasing locally. "Ok now minimise all", "can you restore my tabs" and
  "open folder named X" are matched without the cloud model, so they work during an outage.

Every tool she runs is logged in `data/veronica.log` as `Running tool <name>(...)`, whether it
came from a quick command or the model chose it.

**Sleep and shutdown always ask.** `sleep_pc`, `shutdown_pc` (restart and sign out included)
and `close_window` need a yes/no. Saying "go to sleep", "goodnight", "bye" or "thanks" to her
only ends the conversation. It never touches the PC, and it never quits VERONICA in voice mode.

### Downloading a file (`tools/download_tools.py`)

Give VERONICA a direct link and she'll download the file to your Downloads folder — "download
https://example.com/report.pdf", "grab the installer from <link>". She **asks first** (it's a
yes/no action), states the filename and size when done, and never runs what she downloaded.

What she can't do, by design: she makes a plain web request with **no stored logins or
cookies**, so she can't sign in, get past a paywall or DRM, or solve a CAPTCHA — only fetch
what's already openly reachable at the link. That also means she can't pull copyrighted movies
or shows from streaming or torrent sites; for video, ask her to open a service you have
(Netflix, Prime, YouTube) instead. A single file over `download.max_mb` (default 2048) is
refused; change it in the `download:` block of `config.yaml`.

### Backup voice

If ElevenLabs can't speak (its monthly credits are used up, or there's no quota left),
VERONICA switches to a free Microsoft neural voice through `edge-tts`. The default is
`tts.backup_voice: en-GB-SoniaNeural`, British like ElevenLabs' "Alice". She tells you once,
and tries ElevenLabs again after `tts.backup_minutes` (180). If both fail, Windows' built-in
voice speaks, so she never goes silent. The free ElevenLabs plan has 10,000 credits a month,
about 10,000 characters spoken. The greeting is remembered across restarts
(`data/proactive_state.json`), so restarting VERONICA doesn't greet you, and spend credits,
every time.

### Your location

VERONICA asks Windows where the PC is. On a desktop without Wi-Fi or GPS positioning,
Windows only knows it to within about 5 km, worked out from the internet connection.
She used to name the village under that rough point ("Naganahalli", "Kokti"). Now, when
Windows' estimate is rough (worse than 1.5 km), she says only the city, and tells you once
how to be exact. Say **"I live in Indiranagar, Bengaluru"** (or "my area is HSR Layout"):
she looks the area up on OpenStreetMap and saves its exact coordinates. Weather, rain
alerts, the satellite and space views, and God's Eye's "near me" all use them.

### Views of Earth (`integrations/earth_views.py`)

| Say | Opens / does | Project |
|---|---|---|
| "show me the ISS in 3D", "track the Hubble", "open the satellite tracker" | 3D satellite and debris tracker on that object | [KeepTrack](https://github.com/thkruz/keeptrack.space) |
| "show me a satellite view of Kerala today" | That day's real satellite image of the place | [NASA Worldview](https://github.com/nasa-gibs/worldview) |
| "what does India look like from space today?" | Fetches NASA's image and **describes it aloud** (clouds, storms, haze, snow) | NASA GIBS + vision model |
| "show me the wind over India", "air pollution map of Delhi", "ocean currents near Chennai", "rain map" | Live animated globe (wind, temperature, rain, clouds, humidity, pressure, currents, waves, PM2.5/PM10) | [Earth](https://github.com/cambecc/earth) (earth.nullschool.net) |

Imagery uses VIIRS (wide swaths, no gaps over India). Before about 2 pm, "today" means
yesterday's complete picture. Places are looked up at run time, and "here" means your
saved area. Describing an image tries Gemini Flash first, then Flash-Lite, which has its
own free quota.

### World Monitor (`integrations/world_monitor.py`)

[World Monitor](https://github.com/koala73/worldmonitor) is a live global intelligence
dashboard: conflicts, military flights, ships, markets, internet outages, disasters and
country risk. Its hosted API and MCP are paid, but **self-hosted they're free**, so it runs
on this PC in Docker from `external/worldmonitor`, with secrets generated locally in its
`.env`. VERONICA connects to its MCP at `http://localhost:3000/api/mcp` with the operator key
`WORLDMONITOR_KEY` (in `config/.env`). Its tools appear as `wm_*` and are read-only, and
"open World Monitor" shows the dashboard.

Requirements: **Docker Desktop running**. Accept Docker's terms the first time you open it.
The first start builds World Monitor's images, which is slow and happens once. VERONICA
starts the stack in the background when needed and refreshes its data every 60 minutes
(`world_monitor.seed_every_minutes`). It uses a few GB of disk and some RAM while running.
Its own AI briefings need an AI key, which isn't given to it by default.

### Speech recognition: Groq Whisper (`stt.backend: auto`)

Measured on this PC (ElevenLabs speaking test commands with local names):

| Engine | Word errors | Time |
|---|---|---|
| Google free (old default) | 24% | ~1.0 s |
| Groq Whisper large-v3-turbo | 15% | ~1.0 s |
| **Groq Whisper + vocabulary hint** (new default) | **8%** | ~0.9 s |
| faster-whisper base, offline | 21% | ~0.6 s |

`auto` uses Groq Whisper, then Google if Groq fails, then faster-whisper on the PC if
there's no internet. The **vocabulary hint** (`speech/vocabulary.py`) is built from your own
data: her name, Jev, your area, your routine and ability names, and the words you taught
her. Whisper invents text for silence and noise (it gave "S.T.A.R.S." and "Rek, Bengaluru"
in tests). Those clips are rejected by confidence score and shape before VERONICA acts, and
the microphone stage already drops most silence and noise. Audio now goes to Groq instead
of Google.

### Live feature check (`scripts/feature_check.py`)

```
python scripts/feature_check.py
```

This builds the real VERONICA and checks every feature against the real services. The
report goes to `data/feature_check.md`. What it does with each kind of feature:
- **Runs** the information features: the brain (including a simulated Gemini outage handled
  by Groq), weather, news, Wikipedia, maths, diagnostics, briefing, memory, to-dos,
  reminders, calendar, routines, coding, Jev, privacy, self-heal, downloads (to a temp
  folder), the voice round trip (ElevenLabs speaks, Google hears), microphone, wake word and
  God's Eye.
- **Routes only** anything that would change your PC (open apps or sites, volume, windows,
  lock/sleep/shutdown, close): it checks your words reach the right command, without running
  it.
- **Checks permission** on destructive actions. Every permission question is answered "no"
  automatically, so nothing destructive can run.
- **Skips** screen, webcam and notifications, so a test never sends your personal content to
  the cloud.
- Removes everything it creates: a test reminder, calendar event, routine, memory fact and
  correction.

Last run: 87 passed, 0 failed, 1 skipped.

### God's Eye: public cameras worldwide

God's Eye shows official public road and weather cameras published as open data by
transport authorities: the US, Canada, the UK, Finland, Norway, Estonia, Catalonia,
Australia (NSW), and now **Hong Kong** (Transport Department, ~850 cameras), **New
Zealand** (Waka Kotahi, ~300) and **Singapore** (LTA). Say "show me public cameras in Hong
Kong" or "what does the traffic camera near Auckland show?". Only official public feeds are
used, never unsecured private cameras. India has no official open camera feed yet, so there
are no Indian cameras. To show more than the default 300 Hong Kong cameras, set
`CCTV_HONGKONG_MAX_SOURCES` (and similar) in God's Eye's environment.

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

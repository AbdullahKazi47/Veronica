# VERONICA

**V**oice-**E**nabled **R**easoning **O**rchestrator for **N**atural **I**nteraction, **C**ontrol & **A**utomation

A voice-first personal AI assistant for Windows, in the spirit of JARVIS. Say **"Veronica"**,
talk to her like a person, and she answers out loud — and actually does things on your PC:
opens apps, manages windows, plans and runs multi-step tasks, writes and runs code, keeps track
of your calendar, watches the world for you, and remembers what matters to you.

She is built as real software, not one big script: a thin voice/text front-end over an
orchestrator, a permission-gated tool registry, layered memory, and a model router — so new
abilities can be added without touching the core.

```
"Veronica."                       ->  she starts listening
"What's on my calendar tomorrow?" ->  "Two things, boss — dentist at ten, and Sara's birthday."
"Close Chrome and open VS Code."  ->  does both, in order
"Should I take the bus or metro?" ->  Jev weighs it up and gives a straight answer
"Veronica" (while she's talking)  ->  she stops mid-sentence and listens
```

---

## Contents

- [What she can do](#what-she-can-do)
- [How it works](#how-it-works)
- [Safety and privacy](#safety-and-privacy)
- [Requirements](#requirements)
- [Install](#install)
- [Run](#run)
- [Check that everything works](#check-that-everything-works)
- [Configuration](#configuration)
- [Training your own wake word](#training-your-own-wake-word)
- [Optional integrations](#optional-integrations)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)
- [Credits](#credits)

---

## What she can do

**Talk naturally**
- Custom-trained **"Veronica" wake word** (openWakeWord, ONNX) — then the conversation stays open,
  no need to repeat her name every turn.
- **Barge-in:** say "Veronica" while she's speaking and she stops to listen.
- Accurate speech recognition with automatic fallback: Groq Whisper → Google → offline faster-whisper.
- Natural voice output (ElevenLabs by default; edge-tts, Fish Audio, piper or pyttsx3 as alternatives).
- Learns words she mishears ("when I say X, I mean Y").
- **Understands what you mean, not the exact words:** "catch me up", "duniya mein kya chal raha hai" and
  "what's the news" all give the news; "Chrome band karo" = "shut the browser" = "close Chrome". When a
  line could mean two things she asks which (98% of 172 test phrasings routed right, up from 47%).
- A real personality: concise, dry wit, answers first, anticipates the next question.

**Control your PC**
- Open apps, files and folders; switch, minimise, close windows; volume; lock, sleep, shut down.
- **Tabs** in Chrome, Edge, Brave, Firefox, File Explorer and VS Code: list, switch, close one or all
  (asks first), and she re-checks afterwards — "Closed 5 of 6 Chrome tabs; one is still open".
- **Folders by name, anywhere** (every drive, not just your user folder): "open placement"; asks which
  when two fit, remembers the answer. Drives and File Explorer: "open D drive", "go into docs",
  "go back", "what's in this folder?". She says only what she verified.
- Semantic computer control through Windows UI Automation (find the button named X → click it),
  with screenshots/OCR as fallback and every action verified afterwards.
- Look at your screen or through the webcam, read notifications — only when you ask.
- Downloads from direct links (asks first).
- **Fix-it:** "what's wrong with this?" / "this game won't open". She reads the error, explains the cause and a
  numbered fix; on "fix it" she downloads the official Microsoft fix (asks first), checks its signature,
  installs it (asks first) and checks it worked. Never DLL sites or cracks.

**Get things done**
- **Agentic tasks:** goal → plan → execute → verify, with diagnosis and re-planning when a step fails.
- **Routines:** one phrase runs several commands ("good night", "work mode").
- **Background tasks:** "research X in the background" — she reports back when done.
- **Jev**, the decision layer: structured pros/cons/weights for "should I X or Y?".
- **Coding:** writes, runs and fixes programs in their own project folder.
- **New abilities by voice:** "Veronica, add flight tracking" — she writes a sandboxed plugin,
  inspects it, and only activates it after you approve.

**Know things**
- Weather, news (your region + world), web search, YouTube, calendar (Google calendars read-only
  via iCal, plus her own events).
- **"What's happening?"**: one spoken briefing covering rain across India, quakes and cyclones (God's Eye),
  volcanoes, WHO disease outbreaks and the headlines. Also "is it raining anywhere in India?", "any
  volcanoes erupting?", "is this ship sanctioned?" (OFAC), and "open / activate God's Eye".
- Live world data via optional integrations: aircraft, satellites, earthquakes, ships, conflicts,
  markets, outages, country risk; 3D satellite tracking and NASA imagery.
- `learn_topic`: studies a subject and keeps notes for later.

**Remember and look after you**
- Short-term conversation memory (survives restarts for a few hours), long-term personal facts,
  and episodic memory of past tasks that informs future plans.
- **Proactive:** speaks up about low battery, a pinned CPU, low disk space or rain; greets you when
  you come back.
- **Self-healing:** restarts dead background workers and restores corrupted memory files from backup.

---

## How it works

```
 mic ──► shared mic stream ──► wake word ("Veronica") ──► speech end detection ──► STT
                                                                                    │
                                                                                    ▼
 speaker ◄── TTS ◄── reply ◄── Orchestrator ◄── fast intent routing / model router (Gemini → Groq)
                                   │
                                   ├── planner → task executor → verify → diagnose/replan
                                   ├── tool registry ──► PermissionGate ──► tools (PC, web, files…)
                                   └── memory: short-term · long-term · episodic · knowledge
```

- **Fast path first:** common commands are matched deterministically and never wait on a model.
- **Model routing:** each request is classified (fast / reasoning / coding / vision) and sent to the
  best configured model, with a capability-preserving fallback chain if one is down or slow.
- **One brain, many front-ends:** the voice loop, the text CLI and the Stitch GUI all call the same
  `Orchestrator.handle()`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

---

## Safety and privacy

VERONICA can act on your computer, so the guard rails are part of the design, not an afterthought:

- **Every tool has a permission level.** Anything above the configured threshold
  (`safety.require_confirmation_above`) is confirmed with you first. Nothing — memory, learning,
  routing, screen text — can grant or bypass a permission.
- **Screen, camera and message content is untrusted data.** She reports it; she never obeys it.
- **Unknown is never success.** An action that ran but couldn't be verified is reported as such.
- **Privacy guard:** passwords, card/account numbers, government IDs and keys are scrubbed before
  anything is sent to a cloud model, and never saved to memory. Ask her "what do you send to the
  cloud?" for a plain answer.
- **Voice-built abilities are sandboxed:** they may only fetch data and return a sentence — no file,
  OS, process or local-network access — and they're inspected before you're asked to activate them.
- **`--dry-run`** simulates risky actions instead of executing them.

> Your requests do go to the cloud model you configure (Gemini/Groq by default). For fully local
> reasoning, set `llm.provider: ollama` and point it at a local model.

---

## Requirements

- **Windows 10/11** (PC control uses Windows UI Automation; text mode runs elsewhere)
- **Python 3.14** (developed and tested on 3.14.5)
- A microphone and speakers for voice mode
- API keys for the services you use (see [Configuration](#configuration))
- Optional: [Docker Desktop](https://www.docker.com/products/docker-desktop/) for World Monitor,
  [Ollama](https://ollama.com) for a local model

**PyAudio is not needed.** All audio goes through `sounddevice`, which installs cleanly on
Python 3.14 without a C++ compiler.

---

## Install

```powershell
git clone https://github.com/AbdullahKazi47/Veronica.git
cd Veronica

# core
pip install -r requirements.txt

# voice: mic, wake word, offline speech recognition
pip install sounddevice numpy onnxruntime openwakeword faster-whisper
python -c "import openwakeword.utils as u; u.download_models()"   # one-time feature models
```

Then create `config/.env` (it is git-ignored) with the keys you need:

```ini
GEMINI_API_KEY=...          # main brain + screen reading
GROQ_API_KEY=...            # backup brain + Whisper speech recognition
ELEVENLABS_API_KEY=...      # voice (or switch tts.backend to edge_tts — no key)

# optional
CALENDAR_ICS_URLS=https://...ics,https://...ics   # Google Calendar "secret address in iCal format"
WORLDMONITOR_KEY=...
```

---

## Run

| How | Command |
|---|---|
| **One click** (wake-word voice mode) | double-click **`Start VERONICA.bat`** |
| Wake-word voice mode | `python -m veronica.main --wake` |
| Voice, always listening (no wake word) | `python -m veronica.main --voice` |
| Text only | `python -m veronica.main` |
| Simulate risky actions | add `--dry-run` |
| Use another config | add `--config path\to\config.yaml` |

Always run from the project root (`...\Veronica`), not from inside the `veronica\` package folder.

---

## Check that everything works

```powershell
python voice_check.py            # mic levels, one speech-to-text pass, wake-word model + live score
python voice_check.py --skip-stt # wake word only
python scripts\feature_check.py  # every feature, safely (destructive actions are never run)
python -m pytest                 # unit tests (no mic, network or GPU needed)
```

`feature_check.py` writes its report to `data/feature_check.md`.

---

## Configuration

Non-secret settings live in [`config/config.yaml`](config/config.yaml); secrets live only in
`config/.env`. The settings you're most likely to touch:

| Setting | What it does |
|---|---|
| `llm.model`, `routing.models` | Which models answer, and the fallback order |
| `stt.backend` | `auto` (Groq Whisper → Google → offline), `google_free`, `faster_whisper`, `vosk` |
| `stt.mic_device_name` | Microphone chosen by name, so Windows renumbering devices can't break it |
| `stt.min_speech_rms` | Loudness floor for "you started talking" — lower it for a quiet mic |
| `stt.pause_threshold` | Silence that ends your sentence (lower = faster replies) |
| `voice.wake_threshold` | Wake-word sensitivity (lower = more sensitive, more false wakes) |
| `voice.conversation_mode` | Keep the conversation open after "Veronica" |
| `voice.barge_in` | `wake_word` to interrupt her by name, `off` to disable |
| `tts.backend` | `elevenlabs`, `edge_tts`, `fish_audio`, `piper`, `pyttsx3` |
| `safety.require_confirmation_above` | `SAFE` · `MODERATE` · `SENSITIVE` · `CRITICAL` |
| `proactive.*` | Battery / CPU / disk / rain alerts and the welcome-back greeting |
| `privacy.*` | What the privacy guard redacts, plus your own patterns |
| `assistant.persona` | Her personality |

---

## Training your own wake word

The bundled model was trained for "Veronica". To retrain it — for your accent, or a different name:

1. Record yourself: `python record_wake_samples.py` (40 "Veronica" clips + 25 other phrases, zipped).
2. Open [`VERONICA_WAKE_TRAINING_COLAB.ipynb`](VERONICA_WAKE_TRAINING_COLAB.ipynb) in Google Colab,
   upload the zip, and run all cells.
3. Export the model as **one self-contained `.onnx` file** (weights embedded, no `.onnx.data`), and put
   it at `veronica/models/veronica_wake_model.onnx`.
4. Run `python voice_check.py --skip-stt` and tune `voice.wake_threshold` from the live score.

More detail: [`docs/training/COLAB_WORKFLOW.md`](docs/training/COLAB_WORKFLOW.md).

---

## Optional integrations

Third-party apps live in `external/` (git-ignored) and are off unless configured.

| Integration | What it adds | Setup |
|---|---|---|
| [God's Eye View](https://github.com/bilawalsidhu/gods-eye-view) | Aircraft, satellites, earthquakes, ships, public cameras on a 3D globe | clone to `external/gods-eye-view`; `gods_eye.enabled: true` |
| [World Monitor](https://github.com/koala73/worldmonitor) | Conflicts, markets, outages, disasters, country risk | clone to `external/worldmonitor`, `docker compose up -d`; `world_monitor.enabled: true` |
| KeepTrack · NASA Worldview · Earth globe | 3D satellite tracking, satellite imagery, wind/rain/pollution | public sites — nothing to install |
| Google Calendar | Your calendars, read-only | `CALENDAR_ICS_URLS` in `config/.env` |

---

## Project layout

```
Veronica/
├── Start VERONICA.bat          one-click launch (wake-word mode)
├── voice_check.py              staged mic / STT / wake-word diagnostic
├── record_wake_samples.py      record your voice for wake-word training
├── VERONICA_WAKE_TRAINING_COLAB.ipynb
├── config/
│   ├── config.yaml             all non-secret settings
│   └── .env                    your API keys (git-ignored)
├── scripts/feature_check.py    live check of every feature
├── tests/                      unit tests
├── docs/                       architecture and per-feature design docs
└── veronica/
    ├── main.py                 entry point; build_app() wires everything together
    ├── config.py               typed config loader (YAML + .env)
    ├── plugin_api.py           the only API voice-built abilities may use
    ├── core/                   orchestrator, Jev, routines, abilities, background tasks,
    │                           proactive monitor, self-healing, context window management
    ├── automation/             planner, task executor, diagnosis, re-planning, closed loop
    ├── llm/                    Gemini/OpenAI-compatible, Anthropic, Ollama providers; model router
    ├── speech/                 speech-to-text, text-to-speech, vocabulary and corrections
    ├── voice/                  shared mic, wake gate, wake detector, barge-in, VAD
    ├── memory/                 short-term, long-term, knowledge, thread/episodic memory
    ├── learning/               experience store, confidence, controlled adaptation
    ├── tools/                  every capability, as a permission-levelled Tool
    ├── vision/                 screen state, UI Automation, OCR, computer-use loop
    ├── integrations/           calendar, God's Eye View, World Monitor, Earth views
    ├── safety/                 permission gate, privacy guard
    ├── runtime/                provider health, circuit breaker
    ├── ui/                     CLI (text/voice) and the Stitch GUI
    └── models/                 the trained wake-word model
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'veronica'` | Run from the project root, not from inside `veronica\`. |
| `Could not find PyAudio` | You're on an old `stt.py`. Current code doesn't use PyAudio — pull the latest. |
| A package "isn't installed" but you installed it | Two Pythons. Use the one `voice_check.py` prints in stage 0 for both `pip` and running. |
| She doesn't notice you speaking | `python voice_check.py`, then lower `stt.min_speech_rms` or raise mic gain in Windows Sound settings. |
| She wakes on her own / never wakes | Adjust `voice.wake_threshold` using the live score from `voice_check.py --skip-stt`. |
| `Deserialize tensor … out of bounds` loading the wake model | The `.onnx` and `.onnx.data` came from different exports. Re-export as a single file (see [Training](#training-your-own-wake-word)). |
| Replies are slow or fail | A cloud model is overloaded; the router falls back automatically. Check `data/veronica.log`. |

Logs: `data/veronica.log`.

---

## Documentation

| Topic | Doc |
|---|---|
| Getting started | [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) |
| Architecture | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Model routing | [`docs/MODEL_ROUTING.md`](docs/MODEL_ROUTING.md) |
| Streaming replies | [`docs/STREAMING_LLM.md`](docs/STREAMING_LLM.md) |
| Context window management | [`docs/CONTEXT_WINDOW.md`](docs/CONTEXT_WINDOW.md) |
| Thread memory · episodic memory | [`docs/THREAD_MEMORY.md`](docs/THREAD_MEMORY.md) · [`docs/EPISODIC_MEMORY.md`](docs/EPISODIC_MEMORY.md) |
| Hearing you correctly (the speech chain) | [`docs/HEARING.md`](docs/HEARING.md) |
| Free quotas, dead rungs, honest failure | [`docs/STAYING_UP.md`](docs/STAYING_UP.md) |
| Tabs, folders, Explorer, verify-then-report | [`docs/DESKTOP_UNDERSTANDING.md`](docs/DESKTOP_UNDERSTANDING.md) |
| Understanding what you mean (paraphrases, Hinglish) | [`docs/UNDERSTANDING.md`](docs/UNDERSTANDING.md) |
| Fix-it mode (diagnose, plan, ask, verify) | [`docs/FIX_IT_MODE.md`](docs/FIX_IT_MODE.md) |
| World watch: "what's happening?", rain, volcanoes, outbreaks, sanctioned ships | [`docs/WORLD_WATCH.md`](docs/WORLD_WATCH.md) |
| Wake word and VAD · barge-in | [`docs/PHASE_11C_WAKE_VAD.md`](docs/PHASE_11C_WAKE_VAD.md) · [`docs/PHASE_11D_BARGE_IN.md`](docs/PHASE_11D_BARGE_IN.md) |
| Voice runtime | [`docs/PHASE_11E_VOICE_ONLY_RUNTIME.md`](docs/PHASE_11E_VOICE_ONLY_RUNTIME.md) |
| Third-party code and licenses | [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md) |

---

## Credits

VERONICA reuses ideas and patterns from several open-source assistant projects (Prodigy-AI,
J.A.R.V.I.S, OpenJarvis and others) and drives a few open-source apps as optional integrations
(openWakeWord, God's Eye View, World Monitor, KeepTrack, NASA Worldview). Exactly what came from
where, and under which license, is recorded in [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md).

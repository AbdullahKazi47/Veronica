"""
Live check of every VERONICA feature: `python scripts/feature_check.py`

Builds the real VERONICA (real config, real services) and runs each feature:
  RUN    — really executed (information features, plus test items it creates and removes)
  ROUTE  — words -> the right command and arguments, NOT executed (things that would change
           the PC: opening apps, windows, volume, lock/sleep/shutdown, closing windows)
  GATE   — permission level / refusal checked (destructive actions are never run)
  SKIP   — deliberately not run: screen, webcam and notifications would send your personal
           content to the cloud during a test; try those by voice yourself
Permission questions are answered "no" automatically (interactive=False), so nothing
destructive can run. Writes a report to data/feature_check.md.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
logging.disable(logging.WARNING)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # place names like Nāganahalli
except Exception:  # noqa: BLE001
    pass

from veronica.config import load_config  # noqa: E402

RESULTS: list[tuple[str, str, str, str, float]] = []   # (area, feature, status, detail, seconds)


def record(area, feature, status, detail="", secs=0.0):
    RESULTS.append((area, feature, status, (detail or "").replace("\n", " ")[:170], secs))
    mark = {"PASS": "+", "FAIL": "X", "SKIP": "-"}.get(status, "?")
    print(f"[{mark}] {area:<14} {feature:<38} {status:<4} {secs:5.1f}s  {(detail or '')[:110]}", flush=True)


def check(area, feature, fn):
    t = time.monotonic()
    try:
        ok, detail = fn()
        record(area, feature, "PASS" if ok else "FAIL", detail, time.monotonic() - t)
    except Exception as e:  # noqa: BLE001
        record(area, feature, "FAIL", f"{type(e).__name__}: {e} | {traceback.format_exc(limit=2)[-160:]}",
               time.monotonic() - t)


def main() -> int:
    cfg = load_config()
    from veronica.main import build_app
    t0 = time.monotonic()
    app = build_app(cfg, interactive=False)       # permission questions -> "no"
    reg, orch = app.tools, app.orchestrator
    record("startup", "build VERONICA", "PASS", f"{len(list(reg.all()))} tools", time.monotonic() - t0)

    def route(text, tool, args=None):
        def fn():
            t, used = reg.fast_match(text)
            if t is None or t.name != tool:
                return False, f"went to {t.name if t else 'the model (no quick command)'}"
            got = t.extract_arguments(used)
            if args and any(got.get(k) != v for k, v in args.items()):
                return False, f"args {got}"
            return True, f"{tool} {got}" if got else tool
        return fn

    def run(text, expect=None, tool=None):
        def fn():
            t, used = reg.fast_match(text)
            if tool and (t is None or t.name != tool):
                return False, f"went to {t.name if t else 'model'}"
            res = reg.dispatch(t.name, **t.extract_arguments(used)) if t else None
            msg = res.message if res else orch.handle(text, orch.new_session()).text
            ok = (res.success if res else True) and (expect(msg) if expect else bool(msg))
            return ok, msg
        return fn

    def gate(tool, at_least):
        from veronica.safety.permissions import PermissionLevel
        def fn():
            t = reg.get(tool)
            lvl = t.permission
            return lvl >= getattr(PermissionLevel, at_least), f"{tool} = {lvl.name}"
        return fn

    # ---------------------------------------------------------------- brain
    def llm_chat():
        r = orch.handle("In one short sentence, what is the capital of Australia?", orch.new_session()).text
        return "canberra" in r.lower(), r
    check("brain", "Gemini answers (routed)", llm_chat)

    def groq_backup():
        from veronica.llm.base import ChatMessage
        backups = getattr(app.llm, "backups", [])
        if not backups:
            return False, "no backup configured"
        r = backups[0].chat([ChatMessage(role="user", content="Reply with just the word: ready")])
        return r.ok and "ready" in (r.content or "").lower(), f"{backups[0].model}: {r.content!r}"
    check("brain", "Groq backup answers", groq_backup)

    def failover():
        import dataclasses
        from types import SimpleNamespace
        from veronica.llm.base import ChatMessage
        from veronica.llm.routing.builder import build_routing
        c2 = load_config()
        c2.routing.models = [dataclasses.replace(m, base_url="http://127.0.0.1:9/v1")
                             if m.model_id.startswith("gemini") else m for m in c2.routing.models]
        asm = build_routing(c2)
        task = SimpleNamespace(task_type="chat", complexity="low", estimated_context_size=0, requires_tools=False,
                               requires_vision=False, requires_streaming=False, explicit_model_request=None,
                               latency_sensitive=False)
        r = asm.router.route(task).chat([ChatMessage(role="user", content="Say: hello")], temperature=0.2)
        used = [a.get("model_id") for a in (r.routing or {}).get("attempts", [])]
        return r.ok and any("gpt-oss" in u for u in used), f"tried {used}"
    check("brain", "Gemini down -> Groq takes over", failover)

    # ---------------------------------------------------------------- everyday info
    check("info", "time", run("what time is it", lambda m: ":" in m, "get_current_time"))
    check("info", "calculator", run("what is 12 times 7", lambda m: "84" in m, "calculate"))
    check("info", "weather (your city)", run("what's the weather", lambda m: "°" in m or "degree" in m.lower(), "get_weather"))
    check("info", "news: India first, then world", run("what's the news", lambda m: len(m) > 60, "get_news"))
    check("info", "Wikipedia", run("who is APJ Abdul Kalam", lambda m: "kalam" in m.lower(), "wikipedia_summary"))
    check("info", "system status", run("system status", lambda m: "%" in m or "percent" in m.lower(), "system_status"))
    check("info", "diagnostics", run("run diagnostics", None, "run_diagnostics"))
    check("info", "daily briefing", run("give me my daily briefing", lambda m: "It's" in m, "daily_briefing"))
    check("info", "read a web page", lambda: (lambda r: (r.success and "Example Domain" in str(r.data), r.message[:80]))(
        reg.dispatch("fetch_webpage", url="https://example.com")))
    check("info", "list a folder", lambda: (lambda r: (r.success and "veronica" in str(r.data).lower(), r.message[:80]))(
        reg.dispatch("list_directory", path=str(ROOT))))

    # ---------------------------------------------------------------- memory & productivity (create, then remove)
    check("memory", "remember a fact", run("remember that my feature check colour is teal",
                                           lambda m: "teal" in m.lower(), "remember_fact"))
    check("memory", "recall it", run("what do you remember about me", lambda m: "teal" in m.lower(), "recall_memory"))
    check("memory", "forget it", run("forget my feature check colour", None, "forget_fact"))
    check("tasks", "add a to-do", run("add check veronica features to my to-do list", None, "todo_list"))
    check("tasks", "remove the to-do", run("remove check veronica features from my to-do list", None, "todo_list"))
    check("tasks", "set a reminder", run("remind me in 45 minutes to finish the feature check",
                                         lambda m: "remind" in m.lower(), "set_reminder"))
    check("tasks", "list reminders", run("what are my reminders", lambda m: "feature check" in m.lower(), "list_reminders"))
    check("tasks", "cancel the reminder", run("cancel the feature check reminder", None, "cancel_reminder"))
    check("calendar", "what's on today", run("what's on today", None, "calendar"))
    check("calendar", "add an event", run("add feature check demo to my calendar tomorrow at 3 pm",
                                          lambda m: "Added" in m, "add_event"))
    check("calendar", "find it tomorrow", run("what's on my calendar tomorrow", lambda m: "feature check" in m.lower(),
                                              "calendar"))
    check("calendar", "remove it", run("remove feature check demo from my calendar", None, "remove_event"))
    check("calendar", "connect help", run("connect my google calendar", lambda m: "iCal" in m, "connect_calendar"))

    # ---------------------------------------------------------------- routines
    check("routines", "create", run("create a routine called feature check: what time is it, then what is 2 plus 2",
                                    lambda m: "2 steps" in m, "create_routine"))
    check("routines", "run by name", run("feature check", lambda m: ":" in m and "4" in m, "run_routine"))
    check("routines", "list", run("what routines do I have", lambda m: "feature check" in m, "routine_info"))
    check("routines", "delete", run("delete the feature check routine", None, "edit_routine"))

    # ---------------------------------------------------------------- learning, coding, Jev, abilities
    check("learning", "what have you learned", run("what have you learned", lambda m: "python" in m.lower(),
                                                   "recall_learning"))
    check("learning", "notes reach the prompt", lambda: (lambda b: (bool(b), b[:90]))(orch._knowledge_block("write some python")))

    def code_and_run():
        from veronica.tools.learning_tools import RunCodeTool, WriteCodeTool, _Projects
        root = Path(tempfile.mkdtemp())
        projects = _Projects(cfg, root=root)
        projects.state_path = root / "state.json"
        w = WriteCodeTool(config=cfg, projects=projects, open_editor=lambda p, f: None)
        res = w.run(task="print the sum of the numbers 1 to 100")
        if not res.success:
            return False, res.message
        r = RunCodeTool(config=cfg, projects=projects).run()
        return r.success and "5050" in r.message, r.message
    check("coding", "write + run a script", code_and_run)
    check("coding", "run_code asks first", gate("run_code", "SENSITIVE"))

    check("jev", "decide (Jev)", run("Jev, should I drink tea or coffee in the evening?",
                                     lambda m: m.startswith("Jev here"), "jev_decide"))
    check("jev", "why", run("Jev, why?", lambda m: "weighted average" in m, "jev_why"))
    check("jev", "how would you handle", run("Jev, how would you handle 'minimise all'?",
                                             lambda m: "window control" in m, "jev_route"))
    check("abilities", "list", run("what abilities have you added", None, "list_abilities"))
    check("abilities", "activation asks first", gate("activate_ability", "SENSITIVE"))
    def ability_safety():
        from veronica.core.abilities import check_code
        return bool(check_code("import os\n")[0]) and bool(check_code("x=().__class__")[0]), "unsafe code rejected"
    check("abilities", "safety check", ability_safety)

    # ---------------------------------------------------------------- self-heal, privacy, speech corrections
    check("self", "self check", run("run a self check", None, "self_heal"))
    check("privacy", "what is sent to the cloud", run("what do you send to the cloud", lambda m: "Gemini" in m,
                                                      "privacy_report"))
    def scrub():
        from veronica.safety import privacy
        out = privacy.get_guard().scrub_text("my password is Hunter22 and card 4111 1111 1111 1111")
        return "Hunter22" not in out and "4111" not in out, out
    check("privacy", "secrets scrubbed", scrub)
    check("speech", "teach a correction", run("when I say zzqx, I mean feature", None, "speech_correction"))
    check("speech", "remove it", run("forget the correction for zzqx", None, "speech_correction"))

    # ---------------------------------------------------------------- download
    def download():
        from veronica.tools.download_tools import DownloadFileTool
        c2 = load_config()
        c2.download.dir = tempfile.mkdtemp()
        r = DownloadFileTool(config=c2).run(url="https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore")
        return r.success, r.message
    check("download", "download a file (temp folder)", download)
    check("download", "asks first", gate("download_file", "SENSITIVE"))
    check("download", "refused without a yes", lambda: (lambda r: (not r.success and "NOT DONE" in r.message, r.message[:80]))(
        reg.dispatch("download_file", url="https://example.com/x.txt")))

    # ---------------------------------------------------------------- views of Earth, World Monitor, location
    for text, tool in [("show me the ISS in 3D", "satellite_tracker_3d"), ("show me a satellite view of Kerala today", "nasa_worldview"),
                       ("show me the wind over India", "earth_globe"), ("show me the air pollution map of Delhi", "earth_globe"),
                       ("open world monitor", "open_world_monitor")]:
        check("earth views", f"'{text}'", route(text, tool))
    check("earth views", "look from space (India)", run("what does India look like from space today",
                                                         lambda m: m.startswith("From space"), "look_from_space"))
    def location():
        from veronica.tools.productivity_tools import Profile
        city, source = Profile(cfg).city_and_source()
        return bool(city) and "," not in (city or "") if source == "detected" else bool(city), f"{city} ({source})"
    check("location", "home area (no made-up village)", location)
    def whisper():
        import requests
        from veronica.speech.stt import SpeechToText
        from veronica.speech.vocabulary import build_vocabulary
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.tts.elevenlabs_voice_id}",
                          params={"output_format": "pcm_16000"}, timeout=30,
                          headers={"xi-api-key": cfg.secret("ELEVENLABS_API_KEY")},
                          json={"text": "Jev, should I take the metro?", "model_id": cfg.tts.elevenlabs_model})
        stt = SpeechToText(cfg.stt)
        stt.vocabulary = lambda: build_vocabulary(cfg)
        res = stt._recognize_groq_whisper(r.content)
        return res.success and "jev" in res.text.lower(), f"Groq Whisper heard {res.text!r} {res.error}"
    check("voice", "Groq Whisper hears 'Jev'", whisper)

    # ---------------------------------------------------------------- PC control: route only, never run
    for text, tool in [("open notepad", "open_application"), ("open youtube in chrome", "open_website"),
                       ("search for cricket scores", "web_search"), ("play lofi on youtube", "play_media"),
                       ("volume to 40", "set_volume"), ("pause the music", "media_control"),
                       ("minimise all my tabs", "window_control"), ("restore my tabs", "window_control"),
                       ("open the projects folder", "open_path"), ("lock my pc", "lock_pc"),
                       ("put the laptop to sleep", "sleep_pc"), ("shut down my pc", "shutdown_pc"),
                       ("cancel shutdown", "cancel_shutdown"), ("close spotify", "close_window"),
                       ("take a screenshot", "take_screenshot"), ("find my resume", "find_file"),
                       ("show me the news", "show_news")]:
        check("pc control", f"'{text}'", route(text, tool))
    for tool, lvl in [("sleep_pc", "SENSITIVE"), ("shutdown_pc", "SENSITIVE"), ("close_window", "SENSITIVE"),
                      ("delete_file", "CRITICAL"), ("run_shell_command", "CRITICAL")]:
        check("safety", f"{tool} needs permission", gate(tool, lvl))
    check("safety", "'go to sleep' never sleeps the PC", lambda: (reg.find_fast_match("go to sleep") is None or
                                                                 reg.find_fast_match("go to sleep").name != "sleep_pc",
                                                                 "ends the conversation instead"))
    check("safety", "shutdown refused without a yes", lambda: (lambda r: (not r.success, r.message[:80]))(
        reg.dispatch("shutdown_pc", action="shutdown")))

    for text, tool in [("what's on my screen", "look_at_screen"), ("look through the camera", "look_through_camera"),
                       ("read my notifications", "read_notifications")]:
        check("private", f"'{text}' (route only)", route(text, tool))
    record("private", "screen / camera / notifications", "SKIP",
           "not run: would send your screen, webcam or notifications to the cloud during a test")

    # ---------------------------------------------------------------- voice hardware
    def tts_stt():
        import requests
        import speech_recognition as sr
        key = os.environ.get("ELEVENLABS_API_KEY") or cfg.secret("ELEVENLABS_API_KEY")
        phrase = "Minimise all my tabs."
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.tts.elevenlabs_voice_id}",
                          params={"output_format": "pcm_16000"}, timeout=30,
                          headers={"xi-api-key": key, "Content-Type": "application/json"},
                          json={"text": phrase, "model_id": cfg.tts.elevenlabs_model})
        if not r.ok:
            return False, f"ElevenLabs {r.status_code}: {r.text[:100]}"
        from veronica.speech.stt import SpeechToText
        stt = SpeechToText(cfg.stt)
        for _ in range(2):                      # Google's free endpoint occasionally drops one
            heard = stt._recognize_google(sr, sr.Recognizer(), sr.AudioData(r.content, 16000, 2))
            if heard.success:
                break
        ok = heard.success and "tab" in heard.text.lower()
        alts = f", alternatives {heard.alternatives}" if heard.alternatives else ""
        return ok, f"ElevenLabs said {phrase!r}, Google heard {heard.text!r}{alts} {heard.error}"
    check("voice", "ElevenLabs voice -> Google hears it", tts_stt)

    def mic():
        from veronica.voice.mic_hub import MicHub, rms
        from veronica.voice.microphone import resolve_input_device
        hub = MicHub(resolve_input_device(cfg.stt.mic_device_name, cfg.stt.mic_device_index))
        if not hub.ensure_running():
            return False, hub.failed
        tap = hub.tap(320); tap.open()
        end, n = time.monotonic() + 1.0, 0
        while time.monotonic() < end:
            n += bool(tap.read())
        tap.close(); hub.stop()
        return n > 20, f"{n} audio blocks in 1 s, room level {hub.ambient_rms() and round(hub.ambient_rms())}"
    check("voice", "microphone (shared stream)", mic)

    def wake():
        from veronica.voice.wake_gate import WakeGate
        g = WakeGate(cfg)
        return g.available, g.reason or "wake word model loaded"
    check("voice", "wake word model", wake)

    # ---------------------------------------------------------------- God's Eye (new cameras included)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and reg.get("find_cctv_cameras") is None:
        time.sleep(2)
    if reg.get("find_cctv_cameras") is None:
        record("god's eye", "connect", "FAIL", "God's Eye tools didn't appear within 90 s")
    else:
        record("god's eye", "connect", "PASS", f"{sum(1 for t in reg.all() if getattr(t, 'is_gods_eye', False) or t.name in cfg.gods_eye.tools)} tools")
        def ge(name, text_ok, **args):
            def fn():
                for attempt in range(2):          # live public services hiccup now and then
                    r = reg.dispatch(name, **args)
                    if r.success and text_ok(r.message):
                        return True, r.message
                    time.sleep(3)
                return False, r.message
            return fn
        tool = reg.get("find_cctv_cameras")
        props = list((tool.parameters or {}).get("properties", {}))
        record("god's eye", "camera tool arguments", "PASS", ", ".join(props))
        loc_key = next((k for k in ("area", "location", "place", "query", "near", "city") if k in props), None)
        for place in ("Hong Kong", "Auckland", "Singapore", "London"):
            args = {loc_key: {"place": place}} if loc_key == "area" else ({loc_key: place} if loc_key else {})
            check("god's eye", f"public cameras: {place}",
                  ge("find_cctv_cameras", lambda m, p=place: "no cameras" not in m.lower(), **args))
        check("god's eye", "earthquakes", ge("get_earthquakes", lambda m: len(m) > 10))
        check("god's eye", "satellites overhead", ge("satellites_overhead", lambda m: len(m) > 10))

    # ---------------------------------------------------------------- report
    passed = sum(1 for r in RESULTS if r[2] == "PASS")
    failed = [r for r in RESULTS if r[2] == "FAIL"]
    skipped = sum(1 for r in RESULTS if r[2] == "SKIP")
    lines = [f"# VERONICA feature check — {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             f"**{passed} passed, {len(failed)} failed, {skipped} skipped** of {len(RESULTS)} checks.", "",
             "| Area | Feature | Result | Detail | s |", "|---|---|---|---|---|"]
    lines += [f"| {a} | {f} | {s} | {d.replace('|', '/')} | {t:.1f} |" for a, f, s, d, t in RESULTS]
    out = ROOT / "data" / "feature_check.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped. Report: {out}")
    for r in failed:
        print(f"  FAILED {r[0]} / {r[1]}: {r[3]}")
    try:
        svc = getattr(app, "gods_eye", None)
        if svc is not None and hasattr(svc, "stop"):
            svc.stop()
    except Exception:  # noqa: BLE001
        pass
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

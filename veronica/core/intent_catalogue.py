"""
The intent catalogue: what each capability is FOR, in many ways of saying it.

Each entry: intent name, the tool it runs, fixed arguments, a slot to fill from the words,
and example phrasings — formal, casual, indirect, questions, Hinglish in Latin script, and
the mishearings speech recognition actually produces. A tool can add its own entries with
an `intents` class attribute (a list of the same dicts); phrasings learned from "no, I
meant ..." are added from data/intent_examples.json.

The paraphrase test set (tests/paraphrase_set.py) is kept separate on purpose: these
examples teach, those phrasings measure.
"""
from __future__ import annotations

from veronica.core.intents import Intent

CATALOGUE: list[dict] = [
    {"name": "world_news", "tool": "get_news", "say": "the news",
     "examples": ["what's the news", "tell me the latest news", "any news today", "give me the headlines",
                  "catch me up on the news", "world news please", "news update", "aaj ki khabar batao", "news sunao",
                  "whats the newz", "read me the headlines", "top stories today", "what's in the news",
                  "any news from india", "latest headlines please", "what are the papers saying",
                  "news from around the world", "world news headlines"]},
    # 2026-11-05: the user asked that "what's happening?" be answered from God's Eye and the rest
    # (rain across India, quakes, storms, volcanoes, outbreaks) with the headlines, not news alone.
    {"name": "situation_report", "tool": "situation_report", "say": "a briefing on what's happening",
     "examples": ["what's happening", "what's happening around the world", "what's going on in the world today",
                  "what's going on in india", "anything big happening", "what's going on out there",
                  "duniya mein kya chal raha hai", "india mein kya ho raha hai", "give me a situation report",
                  "world update", "brief me on current events", "what's new in the world",
                  "any updates from around the world", "what happened today in the world", "catch me up",
                  "what's happening is it raining anywhere", "status of the world", "what's going on right now"]},
    {"name": "rain_now", "tool": "rain_now", "say": "where it's raining",
     "examples": ["is it raining anywhere in india", "where is it raining", "is it raining somewhere",
                  "rain across india", "which cities have rain", "any rain in india right now", "where's it pouring",
                  "kahan baarish ho rahi hai", "is there rain anywhere", "is it raining in mumbai and delhi",
                  "rain anywhere in the country"]},
    {"name": "volcanoes", "tool": "volcano_activity", "say": "volcano activity",
     "examples": ["any volcanoes erupting", "volcano news", "which volcanoes are active", "is any volcano erupting",
                  "eruptions this week", "volcanic activity", "is etna erupting", "volcanoes in indonesia"]},
    {"name": "outbreaks", "tool": "disease_outbreaks", "say": "disease outbreaks",
     "examples": ["any disease outbreaks", "outbreak news", "is there a new virus", "what diseases are spreading",
                  "who outbreak alerts", "any epidemic going on", "ebola news", "is there an outbreak in india",
                  "koi bimari phail rahi hai"]},
    {"name": "ship_sanctions", "tool": "ship_sanctions", "say": "a ship sanctions check",
     "examples": ["is this ship sanctioned", "is the tanker on the sanctions list", "check if the vessel is sanctioned",
                  "sanctioned ships", "is imo 7406784 sanctioned", "is that boat under sanctions"]},
    {"name": "open_gods_eye", "tool": "open_gods_eye_view", "say": "opening God's Eye",
     "examples": ["open god's eye", "activate god's eye", "turn on gods eye", "launch god's eye view",
                  "show me the globe", "bring up god's eye", "start the 3d globe", "gods eye kholo", "open godseye",
                  "fire up the world view", "god's eye chalu karo"]},
    {"name": "calendar_today", "tool": "calendar", "slot": "when", "say": "what's on your calendar",
     "examples": ["what's on my calendar", "what do I have today", "am I busy today", "do I have any meetings",
                  "what's my schedule", "what's planned for today", "anything on my agenda", "what's next on my calendar",
                  "do I have anything tomorrow", "am I free this afternoon", "what's my day look like",
                  "aaj mera schedule kya hai", "any appointments today", "check my calendar", "how busy am I today"]},
    {"name": "close_app", "tool": "close_window", "slot": "app", "say": "closing a window",
     "examples": ["close chrome", "shut chrome", "quit spotify", "exit notepad", "close the browser",
                  "get rid of the chrome window", "kill spotify", "close that window", "chrome band karo",
                  "spotify band kar do", "close whatsapp", "shut down word", "close the app", "close notepad window"]},
    {"name": "volume_down", "tool": "set_volume", "args": {"action": "down"}, "say": "turning the volume down",
     "examples": ["turn the volume down", "lower the volume", "it's too loud", "make it quieter", "volume down",
                  "decrease the volume", "too noisy", "bring the volume down", "volume kam karo", "awaaz kam karo",
                  "softer please", "reduce the sound", "that's way too loud", "lower the sound", "awaaz thoda kam kar do"]},
    {"name": "volume_up", "tool": "set_volume", "args": {"action": "up"}, "say": "turning the volume up",
     "examples": ["turn the volume up", "louder", "make it louder", "volume up", "increase the volume",
                  "I can't hear it", "it's too quiet", "raise the volume", "awaaz badhao", "volume zyada karo",
                  "pump up the volume", "more volume", "boost the sound", "i can barely hear anything"]},
    {"name": "mute", "tool": "set_volume", "args": {"action": "mute"}, "say": "muting",
     "examples": ["mute", "mute the sound", "silence please", "turn off the sound", "mute the volume",
                  "no sound", "kill the sound", "awaaz band karo", "shut the sound off", "mute it", "kill the audio"]},
    {"name": "open_folder", "tool": "open_path", "slot": "name", "args": {"kind": "folder"}, "say": "opening a folder",
     "examples": ["open the placement folder", "show me my projects folder", "take me to my documents folder",
                  "open my downloads", "go to the photos folder", "open folder placement", "show me the folder",
                  "placement folder kholo", "open my college notes folder", "bring up the projects directory",
                  "take me to placement", "where is my placement folder open it"]},
    {"name": "open_app", "tool": "open_application", "slot": "name", "say": "opening an app",
     "examples": ["open spotify", "launch chrome", "start notepad", "open whatsapp", "fire up vs code",
                  "can you open the calculator", "open excel", "start word", "spotify kholo", "chrome chalu karo",
                  "open settings", "launch the camera app"]},
    {"name": "weather", "tool": "get_weather", "say": "the weather",
     "examples": ["what's the weather", "how's the weather today", "is it going to rain", "do I need an umbrella",
                  "how hot is it outside", "weather forecast", "will it rain tomorrow", "aaj mausam kaisa hai",
                  "temperature outside", "is it cold today", "what's it like outside", "is it raining", "rain later today"]},
    {"name": "time", "tool": "get_current_time", "say": "the time",
     "examples": ["what time is it", "tell me the time", "what's the time", "time please", "kitne baje hain",
                  "current time", "do you know what time it is", "what's the time right now", "time kya hua"]},
    {"name": "pc_slow", "tool": "run_diagnostics", "say": "checking why the PC is slow",
     "examples": ["why is my pc slow", "my computer is so slow", "the laptop is lagging", "everything is freezing",
                  "what's slowing down my pc", "pc is hanging", "check my computer", "run diagnostics",
                  "why is my laptop so laggy", "computer slow chal raha hai", "something is eating my cpu",
                  "my pc is sluggish", "how is my pc doing", "pc hang ho raha hai", "check my pc for problems",
                  "why is the system so slow"]},
    {"name": "system_status", "tool": "system_status", "say": "the system status",
     "examples": ["system status", "how much battery do I have", "battery level", "how much ram is used",
                  "cpu usage", "how much disk space is left", "is my battery charging", "battery kitni hai",
                  "battery status", "how much storage do i have", "check the battery", "is my disk full"]},
    {"name": "look_at_screen", "tool": "look_at_screen", "slot": "question", "say": "looking at your screen",
     "examples": ["what's on my screen", "look at my screen", "read what's on the screen", "what am I looking at",
                  "can you see my screen", "explain this page", "summarize what's on screen", "read this email for me",
                  "screen pe kya hai", "what does this window say"]},
    {"name": "fix_problem", "tool": "fix_problem", "slot": "description", "say": "finding what's wrong",
     "examples": ["what's wrong with this", "this isn't working", "why won't this open", "help me fix this",
                  "this game won't start", "the app keeps crashing", "I'm getting an error", "fix this error",
                  "a dll is missing", "msvcp140.dll was not found", "it says something is missing",
                  "why is this not working", "this program won't launch", "yeh kaam nahi kar raha",
                  "game nahi chal raha", "can you figure out what's wrong", "look at this error",
                  "my game crashes on startup", "something went wrong with this app", "it shows an error message"]},
    {"name": "apply_fix", "tool": "apply_fix", "say": "doing the fix",
     "examples": ["fix it", "do it", "go ahead and fix it", "do everything needed", "apply the fix",
                  "yes fix it", "install what's needed", "sort it out", "theek kar do", "go ahead"]},
    {"name": "fix_history", "tool": "fix_history", "say": "what I changed",
     "examples": ["what did you change", "what did you install", "what have you fixed", "show me the fix log",
                  "what changes did you make", "undo that", "how do I undo the fix", "kya change kiya"]},
    {"name": "minimize_all", "tool": "window_control", "args": {"action": "minimize_all"}, "say": "minimising everything",
     "examples": ["minimize everything", "show the desktop", "hide all windows", "clear the screen",
                  "minimise all", "go to desktop", "sab minimize karo", "hide everything"]},
    {"name": "lock", "tool": "lock_pc", "say": "locking the PC",
     "examples": ["lock my pc", "lock the computer", "lock the screen", "lock the laptop", "lock it up",
                  "pc lock karo", "lock my system"]},
    {"name": "list_tabs", "tool": "list_tabs", "say": "listing your tabs",
     "examples": ["what tabs are open", "list my tabs", "which tabs do I have open", "how many tabs are open",
                  "read me my tabs", "kitne tabs khule hain", "show my open tabs list"]},
    {"name": "close_all_tabs", "tool": "close_tab", "args": {"scope": "all"}, "say": "closing all tabs",
     "examples": ["close all tabs", "close every tab", "close all my tabs", "get rid of all the tabs",
                  "sab tabs band karo", "shut all the tabs"]},
    {"name": "reminders", "tool": "list_reminders", "say": "your reminders",
     "examples": ["what are my reminders", "list my reminders", "do I have any reminders",
                  "what did I ask you to remind me", "show reminders", "any reminders set"]},
    {"name": "briefing", "tool": "daily_briefing", "say": "your daily briefing",
     "examples": ["give me my daily briefing", "morning briefing", "brief me for the day", "start my day",
                  "what do I need to know today", "good morning briefing"]},
    {"name": "chat", "tool": None,
     "examples": ["hi", "hello", "how are you", "good morning", "thank you", "thanks a lot", "you're funny",
                  "tell me a joke", "who are you", "what's your name", "i love you", "i'm bored", "i'm tired",
                  "that's cool", "nice", "never mind", "what do you think about that", "i had a long day",
                  "you're the best", "kaise ho", "sab theek", "let's talk", "how was your day", "good night",
                  "i'm back", "i'm out", "what can you do", "who made you", "are you there",
                  "explain quantum physics", "what is the capital of france", "write me a poem",
                  "how do i cook pasta", "what does photosynthesis mean", "can you help me study"]},
]


def build_intents(tools=None) -> list[Intent]:
    """The catalogue, plus any `intents` declared on tools, restricted to tools that exist."""
    names = {t.name for t in tools} if tools is not None else None
    entries = list(CATALOGUE)
    for t in tools or []:
        for extra in getattr(t, "intents", None) or []:
            entries.append({"tool": t.name, **extra})
    out = []
    for e in entries:
        if names is not None and e["tool"] is not None and e["tool"] not in names:
            continue
        out.append(Intent(name=e["name"], tool=e["tool"], examples=list(e["examples"]), args=dict(e.get("args") or {}),
                          slot=e.get("slot", ""), say=e.get("say", "")))
    return out

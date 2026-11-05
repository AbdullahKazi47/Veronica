"""Paraphrase set for measuring understanding — NOT the catalogue's examples (those teach;
these measure). Each intent: phrasings in English, casual, indirect, Hinglish, and as
speech recognition mishears them."""

INTENTS: dict[str, tuple[str, list[str]]] = {
    # intent name: (tool, phrasings)
    "world_news": ("get_news", [
        "what's the news today", "any big news", "headlines please", "fill me in on the news",
        "what's the news around the world", "tell me today's headlines", "news batao", "any news",
        "read the news", "latest news please"]),
    "situation_report": ("situation_report", [
        "what's happening around the world", "anything new going on in the world?", "duniya mein kya chal raha hai",
        "okay tell me what's happening around the world today", "what's going on globally", "what's going on in india",
        "give me a world update", "what's happening? is it raining somewhere in india", "situation report please",
        "what's happening right now"]),
    "rain_now": ("rain_now", [
        "is it raining anywhere in india", "where is it raining right now", "is it raining somewhere in the country",
        "which places have rain", "any rain across india", "kahan baarish ho rahi hai", "where's it raining",
        "is there rain anywhere in india", "rain in india right now", "show me where it rains"]),
    "calendar_today": ("calendar", [
        "what's my day like", "anything on my calendar?", "am I free today", "do I have meetings today",
        "what's on my schedule", "anything planned for tomorrow", "am i busy this evening", "what's next for me today",
        "check my agenda", "aaj kya plan hai"]),
    "close_app": ("close_window", [
        "close chrome", "shut the browser", "get rid of that chrome window", "chrome band karo", "quit notepad",
        "exit spotify", "kill the whatsapp window", "close notepad please", "shut spotify down", "notepad band kar do"]),
    "volume_down": ("set_volume", [
        "it's too loud", "turn it down", "lower the volume", "volume kam karo", "bring it down a bit",
        "make it softer", "the sound is too loud", "decrease volume", "quieter please", "awaaz thodi kam karo"]),
    "volume_up": ("set_volume", [
        "turn it up", "louder please", "i can barely hear it", "increase the sound", "volume badhao",
        "raise the volume a bit", "make it louder", "it's too quiet", "volume up please", "awaaz zyada karo"]),
    "mute": ("set_volume", [
        "mute", "mute the volume", "silence", "turn the sound off", "no sound please", "mute everything",
        "awaaz band karo", "mute it now", "kill the audio", "shut the sound"]),
    "open_folder": ("open_path", [
        "open placement", "show me my placement folder", "take me to placement", "open the projects folder",
        "go to my downloads folder", "placement folder kholo", "open up the notes folder", "take me to my documents",
        "bring up my photos folder", "open my college folder"]),
    "open_app": ("open_application", [
        "open spotify", "launch notepad", "start chrome", "fire up whatsapp", "can you open calculator",
        "spotify kholo", "open the settings app", "please launch excel", "start up word", "open vs code"]),
    "weather": ("get_weather", [
        "how's the weather", "is it raining outside", "do i need an umbrella today", "what's the temperature",
        "mausam kaisa hai", "is it hot outside", "weather today", "will it rain later", "how cold is it",
        "what's the forecast"]),
    "time": ("get_current_time", [
        "what time is it", "time please", "what's the time now", "kitne baje hai", "tell me the time",
        "do you have the time", "current time please", "what time is it right now", "time batao", "whats the tym"]),
    "pc_slow": ("run_diagnostics", [
        "why is my pc slow", "my laptop is lagging", "the computer keeps freezing", "something is slowing my pc down",
        "pc hang ho raha hai", "why is everything so slow", "my system is sluggish", "check what's wrong with my pc",
        "laptop bahut slow hai", "computer is very slow today"]),
    "look_at_screen": ("look_at_screen", [
        "what's on my screen", "look at my screen", "what am i looking at", "read the screen for me",
        "can you see what's on my screen", "screen pe kya likha hai", "summarise this page", "what does this window say",
        "explain what's on screen", "read this for me"]),
    "minimize_all": ("window_control", [
        "minimize everything", "show me the desktop", "hide all the windows", "minimise all", "clear my screen",
        "go to the desktop", "sab minimize kar do", "hide everything please", "minimize all windows", "show desktop"]),
    "lock": ("lock_pc", [
        "lock my pc", "lock the screen", "lock the laptop", "lock my computer", "lock it", "pc lock kar do",
        "lock the system", "please lock my pc", "lock screen now", "lock up the computer"]),
    "list_tabs": ("list_tabs", [
        "what tabs are open", "list my tabs", "how many tabs do i have", "which tabs are open",
        "read out my tabs", "kitne tabs khule hai", "tell me my open tabs", "what tabs do i have open",
        "list the tabs in chrome", "name my tabs"]),
    "fix_problem": ("fix_problem", [
        "what's wrong with this", "this game won't open", "my game says msvcp140.dll is missing", "help me fix this",
        "why does this keep crashing", "i'm getting an error when i open it", "game nahi chal raha",
        "can you figure out why this isn't working", "the app crashes every time", "look at this error message"]),
    "system_status": ("system_status", [
        "how much battery is left", "battery status", "how much ram am i using", "cpu usage", "system status",
        "is the laptop charging", "how much storage is free", "battery kitni hai", "check the battery",
        "how full is my disk"]),
}

CHAT: list[str] = [
    "hi veronica", "how are you doing", "thanks", "you're awesome", "tell me a joke", "good morning",
    "what's your favourite colour", "i'm so tired today", "that's interesting", "who built you", "i'm back",
    "never mind", "kaise ho", "what is love", "explain black holes", "write a short poem about rain",
    "what's the capital of japan", "i had a rough day", "how do i make tea", "are you there",
]

# Lines where two different actions are equally likely: she must ask, never act.
AMBIGUOUS: list[str] = [
    "anything new today", "what's up for today", "close everything", "open it up", "check on things",
    "can you check it", "show me today", "turn everything off", "shut it all down", "clear it all",
]

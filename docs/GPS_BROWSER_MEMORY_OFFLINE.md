# GPS, browser tasks, Mem0 memory and the offline brain (2026-10-17)

## GPS (`veronica/integrations/gps.py`, `veronica/tools/gps_tools.py`)
This laptop (Dell Latitude 5520) has no GPS chip, so VERONICA finds a receiver at run time:

| Source | How | Setup |
|---|---|---|
| USB GPS receiver | any NMEA-over-serial dongle (u-blox etc.) | plug it in; COM ports are scanned every minute |
| Phone over Wi-Fi (UDP) | phone app sends NMEA to this PC, port 10110 | say **"use my phone as GPS"**; she reads out this PC's address. Android: Share GPS / GPSd Forwarder; iPhone: GPS2IP |
| Phone over TCP | app serves NMEA | `gps.tcp: "phone-ip:port"` in config.yaml |

A fresh fix (< 2 min) beats every other location source: weather, "here", the briefing and
Earth views use it. "Where am I?" says the place, how precise it is, and where the fix
came from, or plainly that there is no GPS. Only checksummed GGA/RMC sentences with sane
values are used; UDP packets from outside the local network are dropped. "Stop using my
phone's GPS" closes the port. The choice is remembered in `data/gps.json`.

## Browser tasks (`veronica/integrations/browser_agent.py`, browser-use)
"Use the browser to find ...", "go online and compare ...". Runs in the background (about a
minute) in VERONICA's **own** Chrome/Edge profile (`data/browser_agent_profile`), so it has
no logged-in accounts. Its rules forbid signing in, entering personal or payment details,
buying, booking, sending, posting, submitting forms, accepting terms and CAPTCHAs; it stops
and says what the user must do. Page text is data, never instructions. browser-use's
CAPTCHA solver, auto-installed extensions, telemetry and cloud sync are off. Model: Gemini
Flash-Lite (Flash's free tier allows about 20 requests a day), Groq as backup.

## Mem0 (`veronica/memory/mem0_memory.py`)
After each conversational (LLM) turn, the exchange is queued to Mem0, which writes short
facts ("prefers tea", "exam on Friday") into a local Qdrant store + SQLite in `data/mem0/`.
Each new request looks up the related ones (1.5 s limit, never blocks a turn) and they go
into the prompt labelled as data. Text passes the privacy guard first; turns about
passwords, cards, PINs or IDs are never stored. "What do you remember about me", "forget
..." and "forget everything" cover Mem0 too. Mem0 telemetry is off.

## Offline brain (`veronica/llm/routing/router.py`, role `offline`)
`qwen3.5:0.8b` on Ollama is tried only after every cloud model has failed. It gets her
character, an `[OFFLINE]` note (no tools, no internet, never claim an action), the last six
plain turns, no tool schemas, and thinking off: about 3-6 s on this CPU instead of over a
minute. Quick commands (open apps, volume, lock...) still work offline through the fast path.
Vision falls back to the same model (`llm.offline_vision_model`); the image then never leaves
the PC. `moondream` also works there but was slower and less accurate in testing.

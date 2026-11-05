# World watch: "what's happening?"

Ask VERONICA what's going on and she gives you one spoken briefing. It covers rain across India, earthquakes
and cyclones from God's Eye View, volcanoes, disease outbreaks and the top headlines. India comes first,
then the world.

This came from a reel about **Argos Atlas** (argosatlas.com), a closed, commercial "live world map" website
that needs an account and has no API, so it can't be plugged in. VERONICA already had most of its layers
through God's Eye View and World Monitor. The three she lacked are now read directly from the same public
sources Argos Atlas credits. "Open Argos Atlas" still opens the website itself in your browser.

## What she can answer

| You say | Tool | Source (public, no key, no login) |
| --- | --- | --- |
| "What's happening?", "What's going on in India / the world?", "Give me a situation report" | `situation_report` | everything below, plus God's Eye and the news, summarised |
| "Is it raining anywhere in India?", "Where is it raining?", "Is it raining in Pune and Goa?" | `rain_now` | Open-Meteo: 35 cities and state capitals plus your own place, in one request |
| "Any volcanoes erupting?", "Volcanoes in Indonesia" | `volcano_activity` | Smithsonian / USGS Weekly Volcanic Activity Report |
| "Any disease outbreaks?", "Is there an outbreak in India?" | `disease_outbreaks` | WHO Disease Outbreak News |
| "Is the ship EBANO sanctioned?", "Is IMO 7406784 sanctioned?" | `ship_sanctions` | US Treasury OFAC SDN list (1,568 vessels) |
| "Open God's Eye", "Activate God's Eye", "Turn on God's Eye", "Bring up the globe" | `open_gods_eye_view` | opens the 3D globe |

Ships that God's Eye reports are checked against the OFAC list automatically. A ship is only called
sanctioned when its **IMO or MMSI number** matches exactly. A name match is reported as a name match only,
because ship names repeat and a wrong "sanctioned" would be a false accusation.

## How the briefing works
- Seven sources are fetched at once. On this PC it takes about 4.5 s, measured live on 2026-11-05.
- A source that is down is skipped; the rest still make the briefing.
- The AI model turns the reports into 4–6 spoken sentences. Everything fetched is framed as external data
  to report, never instructions to follow. Without the model she reads the key lines plainly.
- "What's happening? Is it raining somewhere in India?" is two sentences but one question. Tools built to
  answer a whole question claim it before the planner, which would otherwise treat any two-sentence
  request as a multi-step job. Ordinary two-sentence commands still go to the planner.
- "What's the news" still gives the news on its own.

## Honest limits
- The volcano report is **weekly**. She always says which week it covers, so she never presents it as live.
- WHO's server caches each address separately; one page was seen stale, missing the newest report. She
  reads two pages and merges them.
- **Live ships need a free key.** God's Eye's ship tracking uses AISStream, and `AISSTREAM_API_KEY` isn't
  set, so "ships near Mumbai" says it's unavailable until you add one (free at aisstream.io, into the God's
  Eye setup). The sanctions check by name or number works without it.
- World Monitor (wars, conflicts, markets) only answers when its Docker stack is running.
- Rain is sampled at 35+ places, not every town: "no rain" means none at the places checked.
- These are voice answers, not new layers drawn on the God's Eye globe.

## Files
- `veronica/tools/world_watch.py`: the five tools and the sources
- `veronica/integrations/gods_eye.py`: the activation phrases and the sanctions flag on ship answers
- `veronica/core/intent_catalogue.py`: paraphrases and Hinglish ("duniya mein kya chal raha hai",
  "kahan baarish ho rahi hai", "gods eye kholo")
- `tests/test_world_watch.py`: fake feeds only

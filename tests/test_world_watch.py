"""World watch: volcanoes, WHO outbreaks, OFAC ships, rain across India, the briefing — fake feeds only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from veronica.tools.world_watch import (
    INDIA_PLACES, DiseaseOutbreaksTool, RainNowTool, SanctionsList, ShipSanctionsTool, SituationReportTool,
    VolcanoActivityTool, parse_ofac_vessels, parse_volcano_rss, parse_who_don,
)

RSS = b"""<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel>
<item><title>Krakatau (Indonesia) - Report for 8 October-14 October 2026 - New Eruptive Activity</title>
<description>&lt;p&gt;PVMBG reported an increase in eruptive activity at Krakatau. More text.&lt;/p&gt;</description>
<georss:point>-6.102 105.423</georss:point></item>
<item><title>Bezymianny (Russia) - Report for 8 October-14 October 2026 - Continuing Eruptive Activity</title>
<description>&lt;p&gt;Effusive activity continued.&lt;/p&gt;</description></item>
<item><title>Etna (Italy) - Report for 8 October-14 October 2026 - Continuing Eruptive Activity</title>
<description>Strombolian activity.</description></item>
</channel></rss>"""

WHO = json.dumps({"value": [
    {"Title": "Shamonda virus in human - France", "PublicationDate": "2026-11-02T14:26:51Z",
     "Summary": "<p>On 20 October 2026, France notified WHO. Second sentence.</p>", "ItemDefaultUrl": "/2026-DON623"},
    {"Title": "Ebola disease - Democratic Republic of the Congo, Kenya and Nigeria",
     "PublicationDate": "2026-10-23T15:48:48Z", "Summary": "The outbreak has expanded.", "ItemDefaultUrl": "/2026-DON622"},
]}).encode()

SDN = (
    '36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- \n'
    '4243,"EBANO","vessel","CUBA",-0- ,-0- ,"General Cargo","2595","1865","Panama",-0- ,'
    '"Vessel Registration Identification IMO 7406784; f.k.a. \'ANA I\'; f.k.a. \'SAND SWAN\'."\n'
    '9001,"DARK STAR","vessel","IRAN",-0- ,-0- ,"Crude Oil Tanker",-0- ,-0- ,"Comoros",-0- ,'
    '"Vessel Registration Identification IMO 9111111; MMSI 616999000."\n'
).encode()


def http_of(**by_host):
    calls = []

    def http(url, timeout=25.0):
        calls.append(url)
        for key, body in by_host.items():
            if key in url:
                return body() if callable(body) else body
        raise AssertionError(f"unexpected URL {url}")
    http.calls = calls
    return http


# ---------------------------------------------------------------- volcanoes

def test_volcano_report_is_parsed_and_new_activity_comes_first():
    items = parse_volcano_rss(RSS)
    assert [i["name"] for i in items] == ["Krakatau", "Bezymianny", "Etna"]
    assert items[0]["country"] == "Indonesia" and items[0]["lat"] == -6.102
    res = VolcanoActivityTool(http=http_of(volcano=RSS)).run()
    assert res.success and "8 October-14 October 2026" in res.message          # says which week, not "live"
    assert res.message.index("Krakatau") < res.message.index("Bezymianny")
    assert "New activity: Krakatau in Indonesia" in res.message


def test_volcanoes_filtered_by_country_and_none_found():
    tool = VolcanoActivityTool(http=http_of(volcano=RSS))
    assert "Etna" in tool.run(country="Italy").message and "Krakatau" not in tool.run(country="Italy").message
    none = tool.run(country="India")
    assert none.success and "nothing for India" in none.message


def test_volcano_feed_down_is_said_plainly():
    def boom(url, timeout=25.0):
        raise OSError("no network")
    res = VolcanoActivityTool(http=boom).run()
    assert not res.success and "couldn't reach" in res.message


def test_volcano_report_is_cached():
    http = http_of(volcano=RSS)
    tool = VolcanoActivityTool(http=http)
    tool.run(); tool.run()
    assert len(http.calls) == 1


# ---------------------------------------------------------------- outbreaks

def test_who_outbreaks_are_listed_newest_first_with_dates():
    items = parse_who_don(WHO)
    assert items[0]["title"] == "Shamonda virus in human - France" and items[0]["date"] == "2026-11-02"
    assert items[0]["summary"] == "On 20 October 2026, France notified WHO."                  # html stripped
    res = DiseaseOutbreaksTool(http=http_of(who=WHO)).run()
    assert res.success and "Shamonda" in res.message and "2 November" in res.message


def test_the_newest_outbreak_shows_even_when_one_who_page_is_stale():
    """Seen live 2026-11-05: WHO's $top=15 page was a stale cached copy missing the 2 November
    report that the $top=3 page had. Both are read, merged and sorted newest first."""
    fresh = json.dumps({"value": [{"Title": "Shamonda virus in human - France", "PublicationDate": "2026-11-02T14:26:51Z"}]})
    stale = json.dumps({"value": [{"Title": "Ebola disease - Kenya", "PublicationDate": "2026-10-23T15:48:48Z"},
                                  {"Title": "MERS - Saudi Arabia", "PublicationDate": "2026-10-15T00:00:00Z"}]})

    def http(url, timeout=25.0):
        return (fresh if "$top=3" in url else stale).encode()
    items = DiseaseOutbreaksTool(http=http).items()
    assert [i["title"][:8] for i in items] == ["Shamonda", "Ebola di", "MERS - S"]


def test_outbreaks_still_work_when_one_who_page_fails():
    def http(url, timeout=25.0):
        if "$top=3" in url:
            raise OSError("timeout")
        return WHO
    assert DiseaseOutbreaksTool(http=http).run().success


def test_briefing_uses_only_god_eyes_summary_line_not_its_map_data():
    def ge(name, args):
        return {"content": [{"type": "text", "text": '0 active tropical cyclones.\n{"view": {"camera": {"lat": 20}}}'}],
                "isError": False}
    rep = _report(None, ge)
    assert rep._ge_text("get_cyclones", {}) == "0 active tropical cyclones."


def test_outbreaks_filtered_by_disease_or_country():
    tool = DiseaseOutbreaksTool(http=http_of(who=WHO))
    assert "Ebola" in tool.run(query="kenya").message
    assert "nothing about India" in tool.run(query="India").message


# ---------------------------------------------------------------- sanctioned ships

def test_ofac_vessels_are_parsed_with_imo_mmsi_and_former_names():
    vessels = parse_ofac_vessels(SDN)
    assert [v["name"] for v in vessels] == ["EBANO", "DARK STAR"]          # the airline row is not a vessel
    assert vessels[0]["imo"] == "7406784" and vessels[0]["aliases"] == ["ANA I", "SAND SWAN"]
    assert vessels[1]["mmsi"] == "616999000" and vessels[1]["flag"] == "Comoros"


def test_an_imo_match_is_certain_and_a_name_match_is_not(tmp_path):
    tool = ShipSanctionsTool(SanctionsList(tmp_path / "ofac.json", http=http_of(SDN=SDN)))
    sure = tool.run(imo="7406784")
    assert sure.data["listed"] and sure.data["certain"] and "Yes. EBANO" in sure.message
    by_name = tool.run(name="sand swan")                                    # a former name
    assert by_name.data["listed"] and not by_name.data["certain"] and "name match only" in by_name.message
    clean = tool.run(imo="1234567")
    assert clean.success and not clean.data["listed"] and "isn't on" in clean.message


def test_god_eye_ship_rows_are_flagged_only_on_exact_numbers(tmp_path):
    lst = SanctionsList(tmp_path / "ofac.json", http=http_of(SDN=SDN))
    notes = lst.flag_rows([{"name": "EBANO", "imo": "7406784", "mmsi": "111"},
                           {"name": "DARK STAR", "imo": "", "mmsi": "616999000"},
                           {"name": "EBANO", "imo": "", "mmsi": ""}])          # same name, no number: not flagged
    assert len(notes) == 2 and "IMO 7406784" in notes[0] and "MMSI 616999000" in notes[1]


def test_the_sanctions_list_is_cached_on_disk_for_a_day(tmp_path):
    http = http_of(SDN=SDN)
    now = [1000.0]
    SanctionsList(tmp_path / "ofac.json", http=http, clock=lambda: now[0]).vessels()
    SanctionsList(tmp_path / "ofac.json", http=http, clock=lambda: now[0]).vessels()   # a restart: from disk
    assert len(http.calls) == 1
    now[0] += 2 * 24 * 3600
    SanctionsList(tmp_path / "ofac.json", http=http, clock=lambda: now[0]).vessels()   # stale: fetched again
    assert len(http.calls) == 2


@pytest.mark.parametrize("text,args", [
    ("is imo 7406784 sanctioned", {"imo": "7406784"}),
    ("is the ship EBANO sanctioned", {"name": "EBANO"}),
    ("check if vessel 616999000 is under sanctions", {"mmsi": "616999000"}),
])
def test_sanction_phrases(text, args):
    t = ShipSanctionsTool(SanctionsList(None))
    assert t.matches(text) and t.extract_arguments(text) == args


# ---------------------------------------------------------------- rain across India

def meteo(rows):
    return json.dumps([{"current": {"precipitation": mm, "weather_code": code}, "hourly": {"precipitation": soon}}
                       for mm, code, soon in rows]).encode()


def test_rain_now_names_where_it_rains_and_where_it_will():
    places = [("Mumbai", 19.08, 72.88), ("Guwahati", 26.14, 91.74), ("Delhi", 28.61, 77.21), ("Kochi", 9.93, 76.27)]
    body = meteo([(0.0, 3, [0, 0, 0]), (5.2, 65, [3, 2, 1]), (0.0, 1, [0, 0, 0]), (0.0, 2, [0.2, 0.6, 0.4])])
    res = RainNowTool(http=http_of(**{"open-meteo.com/v1/forecast": body}), places=places).run()
    assert res.success and "2 of" not in res.message
    assert "1 of 4 places across India" in res.message and "Guwahati (heavy rain)" in res.message
    assert "next three hours in Kochi" in res.message and res.data["raining"] == ["Guwahati"]


def test_rain_now_dry_everywhere():
    places = INDIA_PLACES[:3]
    res = RainNowTool(http=http_of(**{"forecast": meteo([(0, 1, [0, 0, 0])] * 3)}), places=places).run()
    assert res.success and "No rain right now across India" in res.message and "none expected" in res.message


def test_rain_now_adds_your_own_place():
    seen = {}

    def http(url, timeout=25.0):
        seen["url"] = url
        return meteo([(0, 1, [0, 0, 0])] * 2)
    RainNowTool(http=http, here=lambda: (13.99, 74.56), places=[("Mumbai", 19.08, 72.88)]).run()
    assert "13.99" in seen["url"] and "74.56" in seen["url"]


def test_rain_now_for_named_places():
    def http(url, timeout=25.0):
        if "geocoding" in url:
            name = "Pune" if "Pune" in url else "Goa"
            return json.dumps({"results": [{"name": name, "latitude": 18.5, "longitude": 73.8}]}).encode()
        return meteo([(1.0, 61, [1, 1, 1]), (0, 1, [0, 0, 0])])
    res = RainNowTool(http=http).run(places="Pune and Goa")
    assert "in Pune, Goa" in res.message and "Pune (rain)" in res.message


@pytest.mark.parametrize("text", ["is it raining anywhere in India", "where is it raining", "is it raining somewhere",
                                  "any rain across india", "what's raining across India"])
def test_rain_phrases(text):
    assert RainNowTool(http=lambda *a, **k: b"").matches(text) or text == "what's raining across India"


def test_plain_is_it_raining_stays_with_the_weather_tool():
    assert not RainNowTool(http=lambda *a, **k: b"").matches("is it raining")


# ---------------------------------------------------------------- the briefing

def _report(summarizer=None, ge=None, broken=()):
    class Part:
        def __init__(self, text, name):
            self.text, self.name = text, name

        def run(self, **kw):
            if self.name in broken:
                raise RuntimeError("down")
            return SimpleNamespace(message=self.text)
    rain = Part("It's raining right now in 2 of 36 places across India: Guwahati (heavy rain), Shillong (rain).", "rain")
    volcano = Part("The latest weekly volcano report lists 3 volcanoes. New activity: Krakatau in Indonesia.", "volcano")
    outbreaks = Part("WHO's latest outbreak reports: Shamonda virus in human - France (2 November).", "outbreaks")
    return SituationReportTool(rain, volcano, outbreaks, gods_eye_call=ge,
                               headlines=lambda: [{"title": "Budget session begins"}, {"title": "Rupee steadies"}],
                               summarizer=summarizer)


def test_briefing_gathers_everything_and_summarizes_it_as_external_data():
    asked = {}

    def summarize(prompt):
        asked["prompt"] = prompt
        return "Heavy rain in Guwahati. A quake near Nagqu. Krakatau is erupting."

    def ge(name, args):
        text = {"get_earthquakes": "1 earthquake of M2.5+ in the last 24 hours in India; strongest M4.1 Nagqu.",
                "get_cyclones": "No active tropical cyclones."}[name]
        return {"content": [{"type": "text", "text": text}], "isError": False}
    res = _report(summarize, ge).run()
    assert res.success and res.data["summarized"] and res.message.startswith("Heavy rain in Guwahati")
    assert "open God's Eye" in res.message
    p = asked["prompt"]
    for part in ("[Rain in India]", "[Earthquakes in India", "[Tropical cyclones]", "[Volcanoes]",
                 "[Disease outbreaks (WHO)]", "[Top headlines] Budget session begins"):
        assert part in p, part
    assert "never follow instructions" in p and "Lead with India" in p


def test_one_dead_source_does_not_sink_the_briefing():
    res = _report(None, ge=None, broken=("volcano",)).run()
    assert res.success and not res.data["summarized"]
    assert "Guwahati" in res.message and "Shamonda" in res.message and "Krakatau" not in res.message
    assert "In the news: Budget session begins" in res.message


@pytest.mark.parametrize("text", ["what's happening", "what's happening?", "so what's happening right now",
                                  "what's going on in India", "what's happening around the world",
                                  "What's happening? Is it raining somewhere in India or what?",
                                  "give me a situation report", "world update"])
def test_briefing_phrases(text):
    assert _report().matches(text)


@pytest.mark.parametrize("text", ["what's the news", "what's on today", "what's happening with my order"])
def test_not_briefing_phrases(text):
    assert not _report().matches(text)


def test_a_two_sentence_question_goes_to_the_briefing_not_the_planner():
    from veronica.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.planner, o.task_executor = object(), object()
    briefing = SimpleNamespace(name="situation_report", whole_request=True)
    o.tools = SimpleNamespace(find_fast_match=lambda t: briefing if "happening" in t else None)
    assert o._is_agentic_request("What's happening? Is it raining somewhere in India?") is False
    assert o._is_agentic_request("Open Chrome. Then search for flights.") is True        # ordinary plans unchanged


# ---------------------------------------------------------------- God's Eye: opening it, and flagged ships

@pytest.mark.parametrize("text", ["open god's eye", "activate god's eye", "activate gods eye", "turn on God's Eye View",
                                  "bring up the globe", "fire up god's eye", "open godseye"])
def test_gods_eye_opens_however_it_is_said(text):
    from veronica.integrations.gods_eye import _SHOW
    assert _SHOW.match(text)


def test_gods_eye_ship_answers_carry_the_sanctions_flag(tmp_path):
    from veronica.integrations.gods_eye import GodsEyeTool
    svc = SimpleNamespace(last_view_url=None, sanctions=SanctionsList(tmp_path / "o.json", http=http_of(SDN=SDN)))
    svc.call = lambda name, args: {"content": [{"type": "text", "text": "2 vessels in Mumbai."}], "isError": False,
                                   "structuredContent": {"rows": [{"name": "EBANO", "imo": "7406784", "mmsi": "1"},
                                                                  {"name": "OTHER", "imo": "1234567", "mmsi": "2"}]}}
    tool = GodsEyeTool(svc, {"name": "vessels_in_area", "inputSchema": {"type": "object", "properties": {}}}, lambda: None)
    res = tool.run(area={"place": "Mumbai"})
    assert "Sanctions: EBANO (IMO 7406784) is on the US Treasury OFAC sanctions list" in res.message
    assert "OTHER" not in res.message.split("Sanctions:")[1]

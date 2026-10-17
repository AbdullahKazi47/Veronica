"""KeepTrack / NASA Worldview / Earth views, World Monitor's MCP client, the Whisper
speech chain, and honest location naming. No network, Docker or microphone."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from veronica.integrations import earth_views as ev
from veronica.integrations.world_monitor import WorldMonitorClient, WorldMonitorTool, result_text


class Index:
    def __init__(self): self.opened = []
    def open_url(self, url, browser=""):
        self.opened.append(url)
        return True, "browser"


def places(name):
    return {"kerala": {"name": "Kerala", "lat": 10.5, "lon": 76.2, "bbox": (8.2, 74.8, 12.8, 77.4)},
            "delhi": {"name": "Delhi", "lat": 28.6, "lon": 77.2, "bbox": (28.4, 76.8, 28.9, 77.3)}}.get(name.lower())


# ---------------------------------------------------------------- KeepTrack

@pytest.mark.parametrize("sat,url", [
    ("ISS", "https://app.keeptrack.space/?sat=25544"), ("the hubble", "https://app.keeptrack.space/?sat=20580"),
    ("starlink", "https://app.keeptrack.space/?search=STARLINK"), ("NOAA 20", "https://app.keeptrack.space/?search=NOAA%2020"),
    ("", "https://app.keeptrack.space/"), ("43013", "https://app.keeptrack.space/?sat=43013"),
])
def test_keeptrack_urls(sat, url):
    assert ev.SatelliteTracker3DTool.url_for(sat) == url


def test_keeptrack_opens():
    idx = Index()
    tool = ev.SatelliteTracker3DTool(index=idx)
    assert tool.matches("show me the ISS in 3D")
    assert tool.run(**tool.extract_arguments("show me the ISS in 3D")).success and idx.opened[0].endswith("sat=25544")


# ---------------------------------------------------------------- NASA Worldview

def test_imagery_day_rules():
    morning, evening = dt.datetime(2026, 10, 17, 9), dt.datetime(2026, 10, 17, 18)
    assert ev.imagery_day("today", morning) == dt.date(2026, 10, 16)     # today's swaths not complete yet
    assert ev.imagery_day("today", evening) == dt.date(2026, 10, 17)
    assert ev.imagery_day("yesterday", evening) == dt.date(2026, 10, 16)


def test_worldview_and_snapshot_urls():
    box = ev._pad_bbox((28.4, 76.8, 28.9, 77.3))                     # a city is padded so it's visible
    assert box[2] - box[0] >= 1.5 and box[3] - box[1] >= 1.5
    day = dt.date(2026, 10, 16)
    assert ev.worldview_url(box, day).startswith("https://worldview.earthdata.nasa.gov/?v=")
    snap = ev.snapshot_url(box, day)
    assert "REQUEST=GetSnapshot" in snap and "TIME=2026-10-16" in snap and ev.TRUE_COLOR in snap


def test_worldview_tool_opens_the_place():
    idx = Index()
    tool = ev.NasaWorldviewTool(index=idx, places=places)
    text = "show me a satellite view of Kerala today"
    assert tool.matches(text) and tool.extract_arguments(text)["place"] == "Kerala"
    assert tool.run(**tool.extract_arguments(text)).success and "worldview" in idx.opened[0]


def test_look_from_space_describes_the_image():
    seen = {}

    def describe(question, jpeg):
        seen["q"], seen["jpeg"] = question, jpeg
        return "Clouds cover the coast; inland is clear."
    tool = ev.LookFromSpaceTool(places=places, fetch=lambda url: b"\xff\xd8\xff fake", describe=describe)
    text = "what does Kerala look like from space today?"
    assert tool.matches(text)
    res = tool.run(**tool.extract_arguments(text))
    assert res.success and "Clouds cover the coast" in res.message and seen["jpeg"].startswith(b"\xff\xd8")
    assert not ev.LookFromSpaceTool(places=places, fetch=lambda u: b"", describe=describe).run(place="Atlantis").success


# ---------------------------------------------------------------- Earth (nullschool)

@pytest.mark.parametrize("what,path", [
    ("wind", "wind/surface/level"), ("air pollution", "particulates/surface/level/overlay=pm2.5"),
    ("ocean currents", "ocean/surface/currents"), ("rain", "wind/surface/level/overlay=total_precipitable_water"),
    ("temperature", "wind/surface/level/overlay=temp"), ("waves", "ocean/primary/waves/overlay=significant_wave_height"),
])
def test_earth_modes(what, path):
    assert ev.earth_mode(what) == path


def test_earth_tool_centres_on_the_place():
    idx = Index()
    tool = ev.EarthGlobeTool(index=idx, places=places)
    text = "show me the air pollution map of Delhi"
    assert tool.matches(text)
    assert tool.run(**tool.extract_arguments(text)).success
    assert idx.opened[0] == "https://earth.nullschool.net/#current/particulates/surface/level/overlay=pm2.5/orthographic=77.20,28.60,3000"
    assert not tool.matches("show me my photos")


# ---------------------------------------------------------------- World Monitor MCP client

class _Resp:
    def __init__(self, body, ctype="application/json", status=200):
        self.text, self.status_code, self.headers = body, status, {"content-type": ctype}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Http:
    def __init__(self, reply): self.reply, self.sent = reply, []
    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append((url, json, headers))
        return self.reply(json)


def test_client_handles_json_and_event_stream():
    tools = {"tools": [{"name": "get_country_risk", "description": "Country risk score.",
                        "inputSchema": {"type": "object", "properties": {"country": {"type": "string"}}}}]}
    http = _Http(lambda req: _Resp("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": tools}) + "\n\n",
                                   "text/event-stream"))
    c = WorldMonitorClient("http://localhost:3000", "wm_key", http=http)
    assert c.list_tools()[0]["name"] == "get_country_risk"
    url, body, headers = http.sent[0]
    assert url == "http://localhost:3000/api/mcp" and headers["X-WorldMonitor-Key"] == "wm_key"


def test_tool_wraps_and_reports_errors():
    spec = {"name": "get_country_risk", "description": "Country risk score.", "inputSchema": {"type": "object"}}
    ok = _Http(lambda req: _Resp(json.dumps({"result": {"content": [{"type": "text", "text": "India: moderate risk."}]}})))
    t = WorldMonitorTool(WorldMonitorClient("http://x", "k", http=ok), spec)
    assert t.name == "wm_get_country_risk" and t.permission.name == "SAFE" and not t.matches("anything")
    assert t.run(country="IN").message == "India: moderate risk."
    bad = _Http(lambda req: _Resp("", status=403))
    assert not WorldMonitorTool(WorldMonitorClient("http://x", "k", http=bad), spec).run().success


def test_result_text_truncates():
    big = {"content": [{"type": "text", "text": "x" * 5000}]}
    assert len(result_text(big, 100)) < 130


# ---------------------------------------------------------------- Whisper speech chain

def _stt():
    from veronica.config import Config
    from veronica.speech.stt import SpeechToText
    cfg = Config().stt
    cfg.backend = "auto"
    return SpeechToText(cfg)


@pytest.mark.parametrize("text,logprob,ok", [
    (" Rekhazin", -1.9, False), (" S.T.A.R.S.", -0.5, False), (" Jev, Bengaluru", -0.7, False),
    (" Thanks!", -0.636, True), (" Yes.", -0.645, True), (" Jev, why?", -0.3, True), (" Veronica", -0.3, True),
    (" Open the Layali website folder", -0.21, True),
])
def test_whisper_invented_text_is_rejected(text, logprob, ok):
    res = _stt()._from_whisper({"text": text, "segments": [{"no_speech_prob": 0, "avg_logprob": logprob}]},
                               "VERONICA, Jev, Bengaluru")
    assert res.success is ok, (text, res)


def test_whisper_result_has_no_google_scale_confidence():
    res = _stt()._from_whisper({"text": " Hello", "segments": [{"avg_logprob": -0.2}]})
    assert res.success and res.confidence is None and res.stats["whisper_logprob"] == -0.2


def test_chain_falls_back_from_groq_to_google(monkeypatch):
    from veronica.speech.stt import STTResult
    stt = _stt()
    monkeypatch.setattr(stt, "_capture_utterance", lambda t: (b"\x00\x01" * 8000, ""))
    monkeypatch.setattr(stt, "_groq_key", lambda: "gsk_test")
    monkeypatch.setattr(stt, "_recognize_groq_whisper", lambda pcm: STTResult(success=False, error="Groq Whisper error 503"))
    monkeypatch.setattr(stt, "_recognize_google_pcm", lambda pcm: STTResult(success=True, text="minimise all"))
    assert stt.listen_once().text == "minimise all"


def test_chain_noise_is_an_answer_not_an_outage(monkeypatch):
    from veronica.speech.stt import STTResult
    stt = _stt()
    called = []
    monkeypatch.setattr(stt, "_capture_utterance", lambda t: (b"\x00\x01" * 8000, ""))
    monkeypatch.setattr(stt, "_groq_key", lambda: "gsk_test")
    monkeypatch.setattr(stt, "_recognize_groq_whisper", lambda pcm: STTResult(success=False, error="Not speech (low confidence)"))
    monkeypatch.setattr(stt, "_recognize_google_pcm", lambda pcm: called.append(1) or STTResult(success=True, text="x"))
    res = stt.listen_once()
    assert not res.success and called == []        # noise isn't sent on to another engine


def test_chain_without_groq_key_uses_google(monkeypatch):
    from veronica.speech.stt import STTResult
    stt = _stt()
    monkeypatch.setattr(stt, "_capture_utterance", lambda t: (b"\x00\x01" * 8000, ""))
    monkeypatch.setattr(stt, "_groq_key", lambda: "")
    monkeypatch.setattr(stt, "_recognize_google_pcm", lambda pcm: STTResult(success=True, text="hello"))
    assert stt.listen_once().text == "hello"


def test_vocabulary_is_built_from_the_users_own_data(tmp_path, monkeypatch):
    from veronica.config import Config
    from veronica.core.routines import RoutineStore
    from veronica.speech import vocabulary as voc
    from veronica.speech.corrections import Corrections
    cfg = Config()
    monkeypatch.setattr(type(cfg), "resolve_path", lambda self, rel: tmp_path / rel)
    (tmp_path / "data").mkdir()
    RoutineStore(tmp_path / "data/routines.json").save("work mode", ["open vs code"])
    Corrections(tmp_path / "data/stt_corrections.json").add("lily", "Layali")
    voc._cache.clear()
    words = voc.build_vocabulary(cfg)
    assert words.startswith("VERONICA, Jev") and "work mode" in words and "layali" in words.lower()

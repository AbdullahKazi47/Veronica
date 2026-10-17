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


def _chain(monkeypatch, *, offline, gemini=None, google=None, groq_key="", gemini_key="k"):
    """An "auto" recognizer with every engine stubbed, and a log of which ones ran.

    The offline model must be stubbed or the real one loads and transcribes the fake PCM,
    which is slow and gives a different answer on every machine.
    """
    from veronica.speech.stt import STTResult
    stt = _stt()
    ran = []

    def stub(name, outcome):
        def engine(pcm):
            ran.append(name)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return engine

    monkeypatch.setattr(stt, "_capture_utterance", lambda t: (b"\x00\x01" * 8000, ""))
    monkeypatch.setattr(stt, "_groq_key", lambda: groq_key)
    monkeypatch.setattr(stt, "_gemini_key", lambda: gemini_key)
    monkeypatch.setattr(stt, "_recognize_local_whisper", stub("offline", offline))
    monkeypatch.setattr(stt, "_recognize_gemini",
                        stub("gemini", gemini if gemini is not None
                             else STTResult(success=False, error="not stubbed")))
    monkeypatch.setattr(stt, "_recognize_google_pcm",
                        stub("google", google if google is not None
                             else STTResult(success=False, error="not stubbed")))
    return stt, ran


def _ok(text, logprob=-0.2):
    from veronica.speech.stt import STTResult
    return STTResult(success=True, text=text, stats={"whisper_logprob": logprob})


def test_offline_leads_and_nothing_is_sent_to_the_cloud(monkeypatch):
    """A confident offline read answers on its own — the audio never leaves the PC."""
    stt, ran = _chain(monkeypatch, offline=_ok("minimise all"))
    assert stt.listen_once().text == "minimise all"
    assert ran == ["offline"]


def test_chain_falls_back_to_the_cloud_when_offline_fails(monkeypatch):
    from veronica.speech.stt import STTResult
    stt, ran = _chain(monkeypatch,
                      offline=STTResult(success=False, error="faster-whisper not installed"),
                      gemini=_ok("open the projects folder"))
    assert stt.listen_once().text == "open the projects folder"
    assert ran == ["offline", "gemini"]


def test_google_is_last_because_it_drops_words(monkeypatch):
    from veronica.speech.stt import STTResult
    stt, ran = _chain(monkeypatch,
                      offline=STTResult(success=False, error="no model"),
                      gemini=STTResult(success=False, error="Gemini speech error 429"),
                      google=_ok("hello"))
    assert stt.listen_once().text == "hello"
    assert ran == ["offline", "gemini", "google"]


def test_chain_without_a_gemini_key_still_reaches_google(monkeypatch):
    from veronica.speech.stt import STTResult
    stt, ran = _chain(monkeypatch, offline=STTResult(success=False, error="no model"),
                      gemini_key="", google=_ok("hello"))
    assert stt.listen_once().text == "hello"
    assert ran == ["offline", "google"]      # gemini skipped, no key


def test_chain_noise_is_an_answer_not_an_outage(monkeypatch):
    from veronica.speech.stt import STTResult
    stt, ran = _chain(monkeypatch,
                      offline=STTResult(success=False, error="Not speech (low confidence)"),
                      gemini=_ok("x"), google=_ok("x"))
    res = stt.listen_once()
    assert not res.success and ran == ["offline"]   # noise isn't sent on to another engine


def test_an_engine_that_raises_is_skipped_not_fatal(monkeypatch):
    stt, ran = _chain(monkeypatch, offline=RuntimeError("model file corrupt"),
                      gemini=_ok("volume to forty"))
    assert stt.listen_once().text == "volume to forty"
    assert ran == ["offline", "gemini"]


# -- second opinion: only unsure offline reads go to the cloud --------------------
def test_an_unsure_offline_read_is_checked_against_the_cloud(monkeypatch):
    stt, ran = _chain(monkeypatch, offline=_ok("ask Jeff", logprob=-0.7),
                      gemini=_ok("ask Jev"))
    res = stt.listen_once()
    assert res.text == "ask Jev" and ran == ["offline", "gemini"]
    assert res.stats["escalated_from"] == "ask Jeff"   # says what it overrode


def test_a_confident_offline_read_is_never_second_guessed(monkeypatch):
    stt, ran = _chain(monkeypatch, offline=_ok("ask Jev", logprob=-0.2), gemini=_ok("wrong"))
    assert stt.listen_once().text == "ask Jev"
    assert ran == ["offline"]        # no quota spent, no audio sent


def test_escalation_can_be_turned_off(monkeypatch):
    stt, ran = _chain(monkeypatch, offline=_ok("ask Jeff", logprob=-0.7), gemini=_ok("ask Jev"))
    stt.config.escalate_below_logprob = 0.0
    assert stt.listen_once().text == "ask Jeff"
    assert ran == ["offline"]        # 0 = offline answers alone, nothing leaves this PC


def test_escalation_fires_through_the_real_offline_method(monkeypatch):
    """The regression that mocking hid: escalate without replacing the engine itself.

    Every other test here monkeypatches `_recognize_local_whisper`, which stores a plain
    function on the instance. The shipped code gated escalation on
    `engine is self._recognize_local_whisper`, and a bound method is rebuilt on every
    attribute access — so the comparison was always false and nothing ever escalated,
    while the mocked tests all passed. This one stubs the *decoder* and leaves the real
    bound method in the chain, so only the labels can carry the gate.
    """
    class Segment:
        text, no_speech_prob, avg_logprob = " close Crow", 0.01, -0.7

    class Model:
        @staticmethod
        def transcribe(samples, **kwargs):
            return iter([Segment()]), None

    stt = _stt()
    cloud = []
    monkeypatch.setattr(stt, "_capture_utterance", lambda t: (b"\x00\x01" * 8000, ""))
    monkeypatch.setattr(stt, "_groq_key", lambda: "")
    monkeypatch.setattr(stt, "_gemini_key", lambda: "key")
    monkeypatch.setattr(stt, "_offline_model", lambda: Model())
    monkeypatch.setattr(stt, "_recognize_gemini",
                        lambda pcm: (cloud.append(pcm), _ok("close Chrome"))[1])

    res = stt.listen_once()
    assert res.text == "close Chrome"
    assert cloud, "the unsure offline read never reached the second opinion"
    assert res.stats["escalated_from"] == "close Crow"


def test_a_failed_second_opinion_leaves_the_offline_read_standing(monkeypatch):
    stt, ran = _chain(monkeypatch, offline=_ok("ask Jeff", logprob=-0.7),
                      gemini=RuntimeError("no internet"))
    assert stt.listen_once().text == "ask Jeff"      # unsure beats nothing
    assert ran == ["offline", "gemini"]


# -- the Gemini recognizer itself -------------------------------------------------
def _gemini(monkeypatch, reply, status=200, pcm_seconds=4.0, hint=""):
    """Call _recognize_gemini against a stubbed HTTP reply; returns (result, request)."""
    import veronica.speech.stt as stt_mod
    sent = {}

    class Response:
        status_code = status

        @staticmethod
        def json():
            if isinstance(reply, dict):
                return reply
            return {"candidates": [{"content": {"parts": [{"text": reply}]}}]}

    class FakeRequests:
        @staticmethod
        def post(url, **kw):
            sent.update(url=url, **kw)
            return Response()

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    stt = _stt()
    monkeypatch.setattr(stt, "_gemini_key", lambda: "key")
    if hint:
        stt.vocabulary = lambda: hint
    pcm = b"\x00\x01" * int(8000 * pcm_seconds)
    return stt._recognize_gemini(pcm), sent


def test_gemini_transcript_is_read_out_of_the_reply(monkeypatch):
    res, _ = _gemini(monkeypatch, "open the projects folder")
    assert res.success and res.text == "open the projects folder"
    assert res.stats["stt_engine"] == "gemini"


def test_gemini_gets_the_audio_inline_and_the_vocabulary_hint(monkeypatch):
    _, sent = _gemini(monkeypatch, "hello", hint="Jev, Bhatkal")
    parts = sent["json"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "audio/wav"
    assert parts[1]["inline_data"]["data"]            # base64 payload, not a file path
    assert "Jev, Bhatkal" in parts[0]["text"]
    assert sent["json"]["generationConfig"]["temperature"] == 0.0   # verbatim, not creative
    assert "key" not in sent["url"]                   # the key goes in params, not the URL


def test_gemini_commentary_is_not_treated_as_a_command(monkeypatch):
    """Asked for a transcript, a model sometimes explains itself. A reply far longer than
    the clip could be is commentary, and must never reach the tool router."""
    waffle = ("I'm sorry, I can't quite make out what is being said in this audio clip. "
              "It sounds like there may be background noise. Could you try again?")
    res, _ = _gemini(monkeypatch, waffle, pcm_seconds=2.0)
    assert not res.success and "too long for the clip" in res.error


def test_gemini_hint_echo_is_rejected(monkeypatch):
    res, _ = _gemini(monkeypatch, "Jev, Bengaluru", hint="VERONICA, Jev, Bengaluru")
    assert not res.success and "Not speech" in res.error


def test_gemini_empty_reply_for_silence(monkeypatch):
    res, _ = _gemini(monkeypatch, "   ")
    assert not res.success and "understand" in res.error


def test_gemini_http_error_is_a_clean_failure_not_a_crash(monkeypatch):
    res, _ = _gemini(monkeypatch, "", status=429)
    assert not res.success and "429" in res.error


def test_gemini_applies_the_users_own_corrections(monkeypatch):
    import veronica.speech.stt as stt_mod
    monkeypatch.setattr(stt_mod.SpeechToText, "correct",
                        lambda self, text: text.replace("Veronika", "Veronica"))
    res, _ = _gemini(monkeypatch, "Veronika open Chrome")
    assert res.text == "Veronica open Chrome"


def test_warm_up_loads_the_offline_model_once(monkeypatch):
    stt = _stt()
    loads = []
    monkeypatch.setattr(stt, "_offline_model", lambda: loads.append(1))
    stt.warm_up()
    assert loads == [1]


def test_warm_up_never_breaks_start_up(monkeypatch):
    """A missing model must not stop VERONICA starting — the cloud engines still work."""
    stt = _stt()
    def boom():
        raise OSError("model not downloaded")
    monkeypatch.setattr(stt, "_offline_model", boom)
    stt.warm_up()        # must not raise


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


# ---------------------------------------------------------------- Whisper echoing the vocabulary hint

_HINT = "VERONICA, Jev, Bengaluru"


@pytest.mark.parametrize("text,logprob", [
    ("SILENCIA, Jev, Bengaluru", -0.2),   # seen live: noise came back as the hint
    ("Jev, Bengaluru", -0.1),
    ("VERONICA Jev", -0.1),
    ("Bengaluru", -0.4),
])
def test_hint_echo_is_rejected(text, logprob):
    from veronica.speech.stt import whisper_junk
    assert whisper_junk(text, _HINT, logprob)


@pytest.mark.parametrize("text", ["Jev, why?", "Veronica", "Veronica, what's the weather in Bengaluru",
                                  "Bengaluru weather", "Thanks!", "Is it raining in Bengaluru"])
def test_real_requests_with_hint_words_pass(text):
    from veronica.speech.stt import whisper_junk
    assert whisper_junk(text, _HINT, -0.2) == ""

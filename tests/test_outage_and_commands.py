"""When Gemini is overloaded: retry, speak plainly, never hide what was already done.
Plus: casual phrasing ("ok now minimise all") still reaches the quick commands."""
from __future__ import annotations

import pytest

from veronica.llm import openai_compatible_provider as oc
from veronica.llm.base import ChatMessage


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"{self.status_code} Server Error")
            err.response = self
            raise err


def _ok(text="hello"):
    return _Resp(200, {"choices": [{"message": {"content": text}}]})


def test_overload_is_retried_then_succeeds(monkeypatch):
    import requests
    calls = iter([_Resp(503), _Resp(503), _ok("made it")])
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(calls))
    monkeypatch.setattr(oc, "_RETRY_DELAYS_S", (0.0, 0.0))
    res = oc.OpenAICompatibleProvider("http://x", "k", "m").chat([ChatMessage(role="user", content="hi")])
    assert res.ok and res.content == "made it"


def test_persistent_overload_gives_a_plain_sentence(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(oc, "_RETRY_DELAYS_S", (0.0, 0.0))
    res = oc.OpenAICompatibleProvider("http://x", "k", "m").chat([ChatMessage(role="user", content="hi")])
    assert not res.ok and res.content == oc.BUSY_MESSAGE
    assert "http" not in res.content and "503" not in res.content


def test_no_internet_message(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(oc, "_RETRY_DELAYS_S", (0.0, 0.0))
    res = oc.OpenAICompatibleProvider("http://x", "k", "m").chat([ChatMessage(role="user", content="hi")])
    assert not res.ok and res.content == oc.OFFLINE_MESSAGE


def test_client_errors_are_not_retried(monkeypatch):
    import requests
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or _Resp(400))
    oc.OpenAICompatibleProvider("http://x", "k", "m").chat([ChatMessage(role="user", content="hi")])
    assert len(calls) == 1


def test_a_tool_that_ran_is_reported_even_if_the_model_then_fails():
    """Regression: the PC went to sleep and all the user heard was an error message."""
    from veronica.config import Config
    from veronica.core.orchestrator import Orchestrator
    from veronica.llm.base import LLMProvider, LLMResponse, ToolCall
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.base import Tool, ToolResult
    from veronica.tools.registry import ToolRegistry

    class OpenThing(Tool):
        name = "open_thing"
        description = "open"
        parameters = {"type": "object", "properties": {}, "required": []}
        def run(self, **kw): return ToolResult.ok("Opened the Layali Website folder.")

    class FlakyLLM(LLMProvider):
        name = "flaky"
        def __init__(self): self.n = 0
        def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="open_thing", arguments={})])
            return LLMResponse(content=oc.BUSY_MESSAGE, ok=False)

    cfg = Config()
    cfg.routing.enabled = False
    reg = ToolRegistry(PermissionGate())
    reg.register(OpenThing())
    from veronica.memory.long_term import LongTermMemory
    import tempfile, pathlib
    orch = Orchestrator(config=cfg, llm=FlakyLLM(), tools=reg,
                        long_term_memory=LongTermMemory(pathlib.Path(tempfile.mkdtemp()) / "m.json"))
    reply = orch.handle("please open that thing for me somehow", orch.new_session()).text
    assert "Opened the Layali Website folder" in reply and "overloaded" in reply


# ---------------------------------------------------------------- casual phrasing

@pytest.fixture(scope="module")
def registry():
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    return build_default_registry(PermissionGate())


@pytest.mark.parametrize("text,tool", [
    ("ok now minimise all", "window_control"),
    ("okay so minimize all my tabs", "window_control"),
    ("can you minimise everything", "window_control"),
    ("veronica minimise all", "window_control"),
    ("restore restore all tabs", "window_control"),
    ("hey please restore my tabs", "window_control"),
    ("open folder name layali website", "open_path"),
    ("open the folder called placement", "open_path"),
    ("open folder named projects", "open_path"),
])
def test_casual_phrasing_reaches_quick_commands(registry, text, tool):
    m = registry.find_fast_match(text)
    assert m is not None and m.name == tool, text


def test_restore_all_is_not_an_app_called_all_tabs(registry):
    from veronica.tools.registry import normalize_command
    t = normalize_command("restore restore all tabs")
    assert registry.get("window_control").extract_arguments(t) == {"action": "restore_all"}


def test_folder_name_extraction(registry):
    from veronica.tools.registry import normalize_command
    tool = registry.get("open_path")
    assert tool.extract_arguments(normalize_command("open folder name layali website")) == {
        "name": "layali website", "kind": "folder"}


# ---------------------------------------------------------------- world + home news

def test_general_news_is_home_first_then_world():
    from veronica.config import Config
    from veronica.tools.news_tools import NewsTool
    cfg = Config()
    cfg.assistant.news_region, cfg.assistant.news_world_region = "IN", "US"
    tool = NewsTool(config=cfg)
    asked = []

    def fake(url):
        asked.append(url)
        if "WORLD" in url:
            return [{"title": f"World story {i}", "source": "AP", "summary": ""} for i in range(10)]
        return [{"title": f"India story {i}", "source": "BBC", "summary": ""} for i in range(10)]
    tool._safe_get = fake
    items = tool.collect("")
    assert any("gl=US" in u and "WORLD" in u for u in asked) and any("gl=IN" in u for u in asked)
    assert items[0]["title"].startswith("India") and any(i["title"].startswith("World") for i in items)
    first_world = next(n for n, i in enumerate(items) if i["title"].startswith("World"))
    assert all(i["title"].startswith("India") for i in items[:first_world])   # India block, then world


def test_summary_asks_for_india_then_world():
    from veronica.config import Config
    from veronica.tools.news_tools import NewsTool
    prompts = []
    tool = NewsTool(config=Config(), summarizer=lambda p: prompts.append(p) or "World and India summary.")
    tool._safe_get = lambda url: [{"title": "A story", "source": "AP", "summary": ""}]
    assert tool.run().success
    p = prompts[0]
    assert p.index("India") < p.index("WORLD")

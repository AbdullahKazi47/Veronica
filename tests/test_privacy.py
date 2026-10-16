"""Privacy guard: secrets and sensitive IDs never reach the cloud model; ordinary
wording still passes through so VERONICA can still answer."""
from __future__ import annotations

import pytest

from veronica.llm.base import ChatMessage
from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
from veronica.safety import privacy
from veronica.safety.privacy import PrivacyConfig, PrivacyGuard, REDACTED
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.privacy_tools import PrivacyReportTool
from veronica.tools.registry import build_default_registry


@pytest.fixture
def guard():
    return PrivacyGuard(PrivacyConfig())


# ---------------------------------------------------------------- what gets scrubbed

@pytest.mark.parametrize("text", [
    "my openai key is sk-abcdef1234567890ABCD",
    "token: ghp_ABCDEFGHIJKLMNOPQRST1234",
    "Authorization: Bearer abc123.def456.ghi789",
    "password=hunter2two",
    "here is the jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT",
])
def test_credentials_are_removed(guard, text):
    assert REDACTED in guard.scrub_text(text)


@pytest.mark.parametrize("text,kind", [
    ("my card is 4111 1111 1111 1111", "card"),          # Luhn-valid Visa test number
    ("card 4111-1111-1111-1111 expires soon", "card"),
    ("the cvv is 123", "financial"),
    ("my pin is 4821", "financial"),
    ("account number is 00123456789", "financial"),
    ("my ssn is 123-45-6789", "ssn"),
    ("aadhaar 2345 6789 0123", "aadhaar"),
    ("PAN ABCDE1234F on file", "pan"),
    ("send it to HDFC0001234", "ifsc"),
])
def test_financial_and_government_ids_removed(guard, text, kind):
    assert REDACTED in guard.scrub_text(text)
    assert guard.report().get(kind, 0) >= 1


def test_card_false_positive_is_not_redacted(guard):
    # 16 digits but not Luhn-valid: a random long number must pass through.
    assert REDACTED not in guard.scrub_text("order id 1234 5678 9012 3456")


def test_ordinary_text_is_untouched(guard):
    for ok in ["remind me to call mom at 6pm", "my sister's birthday is 12 March",
               "email abdullah@gmail.com the report", "open the projects folder",
               "what's the weather in Bengaluru"]:
        assert guard.scrub_text(ok) == ok


def test_label_is_kept_value_is_hidden(guard):
    out = guard.scrub_text("the cvv is 123")
    assert "cvv" in out.lower() and "123" not in out


def test_disabled_guard_passes_everything_through():
    g = PrivacyGuard(PrivacyConfig(enabled=False))
    assert g.scrub_text("password=hunter2two") == "password=hunter2two"


def test_extra_patterns_from_config():
    g = PrivacyGuard(PrivacyConfig(extra_patterns=(r"EMP-\d{4}",)))
    assert REDACTED in g.scrub_text("my badge is EMP-4821") and g.report().get("custom") == 1


# ---------------------------------------------------------------- wire-level

def test_scrub_wire_handles_text_and_vision_parts(guard):
    msgs = [
        {"role": "user", "content": "my key sk-abcdef1234567890ABCD please"},
        {"role": "user", "content": [
            {"type": "text", "text": "card 4111 1111 1111 1111"},
            {"type": "image_url", "image_url": {"url": "data:..."}}]},
    ]
    out = guard.scrub_wire(msgs)
    assert REDACTED in out[0]["content"]
    assert REDACTED in out[1]["content"][0]["text"]
    assert out[1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:..."}}
    assert msgs[0]["content"] == "my key sk-abcdef1234567890ABCD please"   # original untouched


def test_provider_scrubs_before_sending(monkeypatch):
    """The real chokepoint: whatever _wire_messages emits is already scrubbed."""
    monkeypatch.setattr(privacy, "_guard", PrivacyGuard(PrivacyConfig()))
    prov = OpenAICompatibleProvider(base_url="http://x", api_key="k", model="m")
    wired = prov._wire_messages([ChatMessage(role="user", content="password=supersecretvalue now")])
    assert REDACTED in wired[0]["content"] and "supersecretvalue" not in wired[0]["content"]


def test_configure_from_config_is_fail_safe():
    # Default module guard redacts even before configure() is called.
    assert REDACTED in privacy.PrivacyGuard().scrub_text("sk-abcdefghij1234567890")
    assert privacy.get_guard() is not None


# ---------------------------------------------------------------- report tool

def test_report_tool_routing():
    reg = build_default_registry(PermissionGate())
    for t in ("what do you send to the cloud", "is my data safe", "have you redacted anything"):
        assert reg.find_fast_match(t).name == "privacy_report", t
    assert PrivacyReportTool.permission < PermissionLevel.SENSITIVE


def test_report_tool_explains_and_counts(monkeypatch):
    g = PrivacyGuard(PrivacyConfig())
    monkeypatch.setattr(privacy, "_guard", g)
    g.scrub_text("my card 4111 1111 1111 1111")
    tool = PrivacyReportTool()
    explain = tool.run(mode="explain")
    assert "Gemini" in explain.message and "card number" in explain.message
    blocked = tool.run(mode="blocked")
    assert "card number" in blocked.message and blocked.data["total"] == 1


def test_report_tool_when_nothing_blocked(monkeypatch):
    monkeypatch.setattr(privacy, "_guard", PrivacyGuard(PrivacyConfig()))
    assert "haven't" in PrivacyReportTool().run(mode="blocked").message.lower()


@pytest.mark.parametrize("text,value", [
    ("my gmail password is Sunflower99!", "Sunflower99"),
    ("passcode: 4821", "4821"),
    ("my api key is sk-xyz", "sk-xyz"),
    ("secret is hunter2", "hunter2"),
])
def test_spoken_secret_label_kept_value_hidden(guard, text, value):
    out = guard.scrub_text(text)
    assert REDACTED in out and value not in out


def test_spoken_secret_no_false_positive(guard):
    for ok in ["I like to pass the ball", "my password's strong", "the secret to good code is tests"]:
        assert REDACTED not in guard.scrub_text(ok)

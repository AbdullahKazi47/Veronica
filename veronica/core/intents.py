"""
Understanding what the user MEANS, not the exact words.

The regex fast paths stay the first, exact layer — but a regex miss no longer means "not
understood", and a regex that grabbed the wrong tool ("what's happening around the world
today" once ran the calendar, because of "today") is overruled when the meaning clearly
points elsewhere.

  catalogue  every intent has a tool, fixed arguments and 10–20 example phrasings
             (core/intent_catalogue.py; a tool may add its own with an `intents` attribute;
             phrasings learned from "no, I meant ..." live in data/intent_examples.json)
  matcher    the request and every example become sparse vectors (words, word pairs and
             character 4-grams, TF-IDF weighted) after a normaliser has mapped synonyms
             and Hinglish ("band karo" -> close, "kam karo" -> lower, "duniya" -> world).
             Local, no model download, about a millisecond.
  decision   high and clear     -> route to the tool directly (arguments from the tool's
                                   own extractor, the intent's fixed args, or a slot finder)
             close between two  -> ONE short question ("the news, or your calendar?")
             medium             -> the model decides, told the likely candidates
             low / small talk   -> the model, as before

Safety is unchanged: this only picks the intent. Every action still goes through
ToolRegistry.dispatch() and the PermissionGate, and a SENSITIVE/CRITICAL tool is never
routed on meaning alone unless the match is high — the gate then asks in full words.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("core.intents")

HIGH = 0.58          # route directly
MID = 0.38           # worth telling the model about
OVERRULE = 0.30      # a regex pick whose own best example scores below this can be overruled
ASK_FLOOR = 0.22     # ...and below HIGH, two different tools at least this likely and
ASK_MARGIN = 0.09    # this close -> one short question instead of a guess


# ------------------------------------------------------------------ normalising

_PHRASES: list[tuple[str, str]] = [
    # Hinglish / Hindi in Latin script (what speech recognition writes down)
    (r"\b(?:kya\s+chal\s+raha\s+(?:hai|he)|kya\s+ho\s+raha\s+(?:hai|he))\b", " what is happening "),
    (r"\b(?:duniya|duniyaa|dunia|vishwa)\b", " world "),
    (r"\b(?:khabar(?:ein|en|e)?|samachar|news\s+batao)\b", " news "),
    (r"\b(?:band|bandh)\s+(?:kar(?:o|do|de|na|dijiye)?|karo)\b", " close "),
    (r"\b(?:kam|dheere|dheema|dheemi)\s+(?:kar(?:o|do|de|na|dijiye)?|karo)\b", " lower "),
    (r"\b(?:zyada|jyada|tez|badha(?:o|do)?|oonchi)\s+(?:kar(?:o|do|de|na|dijiye)?|karo)?\b", " raise "),
    (r"\b(?:khol(?:o|do|na|iye)?|chalu\s+kar(?:o|do)?)\b", " open "),
    (r"\b(?:dikha(?:o|do|iye)?|batao|bata\s+do)\b", " show "),
    (r"\b(?:awaaz|awaz|aawaz|aavaaz)\b", " volume "),
    (r"\b(?:mausam)\b", " weather "),
    (r"\b(?:aaj)\b", " today "),
    (r"\b(?:kal)\b", " tomorrow "),
    (r"\b(?:kitne\s+baje|time\s+kya\s+hua|samay|tym|tyme|taim)\b", " time "),
    (r"\b(?:hang|hanging|atak)\s+(?:ho\s+)?(?:raha|rahi|gaya|gayi)?\b", " slow freezing "),
    (r"\b(?:thoda|thodi|thode|bahut|bohot|zara)\b", " "),
    (r"\b(?:kharab|kaam\s+nahi\s+kar\s+raha|nahi\s+chal\s+raha|chal\s+nahi\s+raha)\b", " not working "),
    (r"\b(?:mera|meri|mere|apna|apni)\b", " my "),
    (r"\b(?:yeh|ye|is|iss)\s+(?=\w)", " this "),
    (r"\b(?:hai|he|hain|ho|hoon|karo|kardo|kar\s+do|please|pls|plz|zara|jara|ji|na|toh|to\s+the)\b", " "),
    # English variants that mean the same thing
    (r"\b(?:get\s+rid\s+of|shut(?:\s+down)?|quit|exit|kill|terminate|close\s+out)\b", " close "),
    (r"\b(?:turn\s+(?:it\s+)?down|turn\s+the\s+volume\s+down|decrease|reduce|quieter|softer|less\s+loud)\b", " lower volume "),
    (r"\b(?:turn\s+(?:it\s+)?up|turn\s+the\s+volume\s+up|increase|louder|crank\s+it)\b", " raise volume "),
    (r"\b(?:too|so|really|very)\s+(?:loud|noisy)\b", " lower volume "),
    (r"\b(?:can'?t\s+hear|can\s+barely\s+hear|hard\s+to\s+hear|too\s+(?:quiet|soft|low))\b", " raise volume "),
    (r"\b(?:audio|sound)\b", " sound "),
    (r"\b(?:storage|disk\s+space|hard\s+disk|drive\s+space)\b", " disk "),
    (r"\b(?:raining|rainy|rain)\b", " rain "),
    (r"\b(?:take\s+me\s+to|bring\s+up|pull\s+up|show\s+me|launch|fire\s+up|start\s+up)\b", " open "),
    (r"\b(?:headlines|news\s+update|current\s+events|current\s+affairs)\b", " news "),
    (r"\b(?:catch\s+me\s+up|fill\s+me\s+in|brief\s+me|update\s+me)\b", " news update "),
    (r"\b(?:schedule|agenda|appointments?|meetings?|plans?)\b", " calendar "),
    (r"\b(?:busy|free)\s+(?:today|tomorrow|tonight)?\b", " calendar free "),
    (r"\b(?:sluggish|laggy|lagging|hanging|freezing|slow(?:ing)?\s+down|so\s+slow|very\s+slow)\b", " slow "),
    (r"\b(?:isn'?t|is\s+not|doesn'?t|does\s+not|won'?t|will\s+not|can'?t|cannot|not)\s+(?:work(?:ing)?|open(?:ing)?|start(?:ing)?|launch(?:ing)?|load(?:ing)?|run(?:ning)?)\b", " not working "),
    (r"\b(?:browser)\b", " browser "),
    (r"\bwhat'?s\b", " what is "), (r"\bit'?s\b", " it is "), (r"\bi'?m\b", " i am "),
    (r"\bwanna\b", " want to "), (r"\bgonna\b", " going to "),
]
_PHRASES_RX = [(re.compile(p, re.I), r) for p, r in _PHRASES]
_STOP = {"the", "a", "an", "my", "me", "please", "can", "could", "would", "will", "you", "u", "is", "are", "to",
         "of", "i", "it", "for", "on", "in", "this", "that", "and", "be", "do", "just", "hey", "veronica", "okay",
         "ok", "so", "now", "right", "some", "tell", "any", "am", "with", "at", "us", "we", "your", "yo", "uh", "um"}


def normalise(text: str) -> str:
    t = " " + (text or "").lower().replace("’", "'") + " "
    for rx, rep in _PHRASES_RX:
        t = rx.sub(rep, t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 + len(suf) - 1 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def features(text: str) -> Counter:
    words = [w.strip("'") for w in normalise(text).split()]
    words = [w for w in words if w]
    content = [_stem(w) for w in words if w not in _STOP]
    f: Counter = Counter()
    for w in content:
        f["w:" + w] += 1.0
    for a, b in zip(content, content[1:]):
        f["b:" + a + "_" + b] += 1.0
    for w in content:
        padded = f"<{w}>"
        for i in range(len(padded) - 3):
            f["c:" + padded[i:i + 4]] += 0.35
    if not content and words:              # "what is it" -> keep something to compare
        for w in words:
            f["w:" + w] += 0.5
    return f


# ------------------------------------------------------------------ the catalogue

@dataclass
class Intent:
    name: str
    tool: Optional[str]                     # None = just conversation (no tool)
    examples: list[str]
    args: dict[str, Any] = field(default_factory=dict)
    slot: str = ""                          # "app" | "name" | "text" | "question" | "query"
    say: str = ""                           # how to name it in a clarifying question


class IntentMatcher:
    """Sparse TF-IDF cosine against every example. An Embedder may replace `vector` (a
    dense model) — tests use the default, which needs no model at all."""

    def __init__(self, intents: list[Intent], embed: Optional[Callable[[str], Any]] = None) -> None:
        self.intents = list(intents)
        self._embed = embed
        self._build()

    def _build(self) -> None:
        self._docs: list[tuple[Intent, Counter]] = [(i, features(e)) for i in self.intents for e in i.examples]
        df: Counter = Counter()
        for _i, f in self._docs:
            df.update(set(f))
        n = max(1, len(self._docs))
        self._idf = {k: math.log((n + 1) / (v + 1)) + 1.0 for k, v in df.items()}
        self._default_idf = math.log(n + 1) + 1.0
        self._vecs = [(i, self._weigh(f)) for i, f in self._docs]

    def add_example(self, intent_name: str, phrase: str) -> bool:
        for i in self.intents:
            if i.name == intent_name and phrase not in i.examples:
                i.examples.append(phrase)
                self._build()
                return True
        return False

    def _weigh(self, f: Counter) -> dict[str, float]:
        v = {k: c * self._idf.get(k, self._default_idf) for k, c in f.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {k: x / norm for k, x in v.items()}

    def rank(self, text: str, top: int = 5) -> list[tuple[Intent, float]]:
        q = self._weigh(features(text))
        best: dict[str, tuple[Intent, float]] = {}
        for intent, v in self._vecs:
            small, big = (q, v) if len(q) < len(v) else (v, q)
            s = sum(x * big.get(k, 0.0) for k, x in small.items())
            if intent.name not in best or s > best[intent.name][1]:
                best[intent.name] = (intent, s)
        return sorted(best.values(), key=lambda p: -p[1])[:top]


# ------------------------------------------------------------------ slots

_LEAD = re.compile(r"^\s*(?:(?:hey\s+)?veronica[,\s]+)?(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|just\s+)*"
                   r"(?:open(?:\s+up)?|take\s+me\s+to|show\s+me|go\s+to|bring\s+up|pull\s+up|launch|start|fire\s+up|"
                   r"get\s+me|find|khol(?:o|do)?)\s+(?:the\s+|my\s+|up\s+)?", re.I)
_TRAIL = re.compile(r"\s+(?:folder|directory|app|application|program|please|for\s+me|khol(?:o|do)?|kholo)\s*[.!?]*\s*$", re.I)


def slot_name(text: str) -> str:
    """'take me to placement' -> 'placement'; 'show me my placement folder' -> 'placement'."""
    t = _LEAD.sub("", text or "", count=1)
    for _ in range(2):
        t = _TRAIL.sub("", t)
    return t.strip(" .!?,'\"")


_APP_NOISE = re.compile(r"\b(?:close|shut|quit|exit|kill|get\s+rid\s+of|the|that|this|my|window|app|application|program|"
                        r"please|band|bandh|karo|kar|do|de|dijiye|now|down|it|of|up)\b", re.I)


def slot_app(text: str, find_window: Optional[Callable[[str], Any]] = None,
             browser_names: Optional[Callable[[], list[str]]] = None) -> str:
    """The app named in 'get rid of that Chrome window' / 'Chrome band karo' -> 'Chrome';
    'shut the browser' -> the browser that has a window open (only if exactly one does)."""
    rest = re.sub(r"\s+", " ", _APP_NOISE.sub(" ", text or "")).strip(" .!?,")
    if re.fullmatch(r"(?:web\s+)?browser", rest, re.I) and browser_names is not None:
        names = browser_names()
        return names[0] if len(names) == 1 else ""
    if not rest or len(rest.split()) > 3:
        return ""
    if find_window is not None:
        try:
            if find_window(rest) is None:
                return ""
        except Exception:  # noqa: BLE001
            return ""
    return rest


# ------------------------------------------------------------------ deciding

_DEICTIC = {"everything", "all", "things", "thing", "stuff", "them", "up", "down", "off", "out", "something",
            "whatever", "there", "here", "these", "those", "lot"}
_VERB_WORDS = {"close": "close", "turn": "turn off" , "shut": "close", "open": "open", "check": "check",
               "clear": "clear", "stop": "stop", "fix": "fix", "show": "show you", "delete": "delete",
               "remove": "remove", "lower": "turn down", "raise": "turn up", "start": "start", "kill": "close"}


def vague_verb(text: str) -> str:
    """'close everything' / 'turn it off' / 'check on things' name an action and no object.
    Returns how to ask about it ('close'), or '' when the request says what it's about."""
    words = [w for w in normalise(text).split() if w not in _STOP and w not in _DEICTIC]
    if len(words) != 1 or words[0] not in _VERB_WORDS:
        return ""
    verb = _VERB_WORDS[words[0]]
    if verb == "turn off" and not re.search(r"\boff\b", text or "", re.I):
        return "turn up or down" if not re.search(r"\b(?:up|down)\b", text or "", re.I) else ""
    return verb

@dataclass
class Decision:
    kind: str                                # "tool" | "ask" | "model"
    tool: Any = None                         # Tool object for "tool"
    text: str = ""                           # the text the tool's extractor should see
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""                          # the clarifying question for "ask"
    hint: str = ""                           # prompt note for "model"
    why: str = ""
    ms: float = 0.0
    intent: str = ""
    score: float = 0.0


def _required_missing(tool, args: dict) -> bool:
    req = list((getattr(tool, "parameters", {}) or {}).get("required", []) or [])
    return any(not args.get(r) for r in req)


class IntentRouter:
    def __init__(self, intents: list[Intent], examples_path: Optional[Path] = None,
                 find_window: Optional[Callable[[str], Any]] = None,
                 browser_names: Optional[Callable[[], list[str]]] = None) -> None:
        self._path = Path(examples_path) if examples_path else None
        self._lock = threading.Lock()
        self._load_learned(intents)
        self.matcher = IntentMatcher(intents)
        self._find_window = find_window
        self._browsers = browser_names

    # -- learned phrasings ("no, I meant ...") -------------------------------------
    def _load_learned(self, intents: list[Intent]) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for i in intents:
            for phrase in (data.get(i.name) or [])[:50]:
                if isinstance(phrase, str) and phrase not in i.examples:
                    i.examples.append(phrase)

    def learn(self, phrase: str, intent_name: str) -> bool:
        phrase = (phrase or "").strip()
        if not phrase or len(phrase) > 120 or not self.matcher.add_example(intent_name, phrase):
            return False
        if self._path is not None:
            with self._lock:
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {}
                except Exception:  # noqa: BLE001
                    data = {}
                data.setdefault(intent_name, [])
                if phrase not in data[intent_name]:
                    data[intent_name].append(phrase)
                from veronica.core import jsonsafe
                self._path.parent.mkdir(parents=True, exist_ok=True)
                jsonsafe.write_json(self._path, data)
        logger.info("Learned that %r means %s.", phrase, intent_name)
        return True

    def intent_for_tool(self, tool_name: str, args: Optional[dict] = None) -> Optional[str]:
        """The catalogue intent a tool call belongs to (for learning from corrections)."""
        args = args or {}
        cands = [i for i in self.matcher.intents if i.tool == tool_name]
        for i in cands:
            if i.args and all(args.get(k) == v for k, v in i.args.items()):
                return i.name
        return cands[0].name if cands else None

    # -- the decision ----------------------------------------------------------------
    def _args_for(self, intent: Intent, tool, text: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        try:
            if tool.matches(text):
                args.update(tool.extract_arguments(text) or {})
        except Exception:  # noqa: BLE001
            pass
        for k, v in intent.args.items():
            args.setdefault(k, v)
        if intent.slot == "app" and not args.get("app") and not args.get("app_name"):
            app = slot_app(text, self._find_window, self._browsers)
            if app:
                args["app_name" if tool.name == "open_application" else "app"] = app
        elif intent.slot == "name" and not args.get("name") and not args.get("app_name"):
            name = slot_name(text)
            if name:
                args["app_name" if tool.name == "open_application" else "name"] = name
        elif intent.slot in ("text", "question", "query", "description", "when") and not args.get(intent.slot):
            args[intent.slot] = text.strip()
        return args

    def decide(self, text: str, registry) -> Decision:
        t0 = time.perf_counter()
        regex_tool, used = registry.fast_match(text)
        ranked = self.matcher.rank(text)
        ms = (time.perf_counter() - t0) * 1000

        def done(d: Decision) -> Decision:
            d.ms = (time.perf_counter() - t0) * 1000
            if ranked:
                d.intent, d.score = ranked[0][0].name, round(ranked[0][1], 3)
            logger.info("Understanding %r -> %s %s (%s %.2f, %.1f ms; %s)", text[:60], d.kind,
                        getattr(d.tool, "name", "") or "", d.intent, d.score, d.ms, d.why)
            return d

        if not ranked:
            return done(Decision("tool", regex_tool, used, why="regex") if regex_tool else Decision("model", why="nothing"))
        top, score = ranked[0]
        runner = next(((i, s) for i, s in ranked[1:] if i.tool != top.tool), (None, 0.0))

        if regex_tool is not None:
            own = max((s for i, s in ranked if i.tool == regex_tool.name), default=0.0)
            if (top.tool and top.tool != regex_tool.name and score >= HIGH and own < OVERRULE
                    and score - own >= 0.25):
                tool = registry.get(top.tool)
                if tool is not None:
                    args = self._args_for(top, tool, text)
                    if not _required_missing(tool, args):
                        return done(Decision("tool", tool, text, args, why=f"meaning overrules regex {regex_tool.name}"))
                    return done(Decision("model", hint=self._hint(ranked), why=f"meaning disagrees with {regex_tool.name}"))
            return done(Decision("tool", regex_tool, used, why="regex"))

        # No exact rule matched: go by meaning.
        if top.tool is None:
            return done(Decision("model", why="conversation"))
        verb = vague_verb(text)
        if verb and score < HIGH:            # "close everything", "turn it off", "check on things"
            return done(Decision("ask", reply=f"What would you like me to {verb}?", why="no object"))
        if (runner[0] is not None and runner[0].tool is not None and runner[1] >= ASK_FLOOR
                and score - runner[1] < ASK_MARGIN):
            return done(Decision("ask", reply=f"Do you mean {top.say or top.name.replace('_', ' ')}, or "
                                              f"{runner[0].say or runner[0].name.replace('_', ' ')}?", why="ambiguous"))
        if score >= HIGH and (score - runner[1] >= ASK_MARGIN):
            tool = registry.get(top.tool)
            if tool is not None:
                args = self._args_for(top, tool, text)
                risky = getattr(getattr(tool, "permission", None), "value", 0) >= 2     # SENSITIVE / CRITICAL
                # A risky action is taken on meaning alone only when the words name its target
                # (an open window called "Chrome"); "get rid of all of it" goes to the model.
                named = bool(top.slot) and any(args.get(k) for k in ("app", "app_name", "name"))
                if not _required_missing(tool, args) and (not risky or named):
                    return done(Decision("tool", tool, text, args, why="meaning"))
        if score >= MID:
            return done(Decision("model", hint=self._hint(ranked), why="medium"))
        return done(Decision("model", why="low"))

    @staticmethod
    def _hint(ranked: list[tuple[Intent, float]]) -> str:
        cands = [(i, s) for i, s in ranked if s >= MID * 0.8][:3]
        if not cands:
            return ""
        parts = [f"{i.tool or 'just talk (no tool)'}" + (f" {i.args}" if i.args else "") + f" ({s:.2f})" for i, s in cands]
        return ("[LIKELY MEANING — from comparing the request with known phrasings] Most likely: " + "; ".join(parts)
                + ". Pick the one the user means (or none, if they are just talking); fill in arguments from their words.")

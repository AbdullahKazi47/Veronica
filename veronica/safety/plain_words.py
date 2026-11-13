"""
Permission questions in plain words: "Are you sure you want to empty the Recycle Bin?" instead
of reading out "run shell command, command powershell -Command Clear-RecycleBin -Force".

Accuracy over polish, on purpose. You approve what she SAYS, so the description is built from the
action itself by fixed rules, never by asking the AI model to summarise it: a model could make a
destructive command sound harmless. A system command she can't read is called just that ("run a
command on your PC that I can't describe"), never guessed. The exact command is still printed in
her terminal and written to the log.
"""
from __future__ import annotations

import ast
import re
from pathlib import PureWindowsPath


def _name(path) -> str:
    """'C:\\Users\\me\\Documents\\report.pdf' -> 'report.pdf' (what a person would call it)."""
    p = str(path or "").strip().strip("'\"").rstrip("\\/")
    if not p:
        return ""
    name = PureWindowsPath(p).name or p
    return name if len(name) <= 60 else name[:57] + "..."


def _app(name: str) -> str:
    """'chrome.exe' -> 'Chrome'."""
    n = re.sub(r"\.exe$", "", (name or "").strip(), flags=re.I)
    return n[:1].upper() + n[1:] if n.islower() else n


def _parse(description: str) -> tuple[str, str, dict]:
    """'<reason> tool({...})' -> (reason, tool, args)."""
    m = re.search(r"(\w+)\((\{.*\}|)\)\s*$", description or "", re.S)
    if not m:
        return (description or "").strip(), "", {}
    reason = (description[:m.start()] or "").strip()
    try:
        args = ast.literal_eval(m.group(2)) if m.group(2).strip() else {}
    except (ValueError, SyntaxError):
        args = {}
    return reason, m.group(1), args if isinstance(args, dict) else {}


UNREADABLE = "run a command I can't put into plain words"

# -- system commands: only what can be read for certain ------------------------------------------
_SHELL_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"\bClear-RecycleBin\b|\$Recycle\.Bin", re.I), "empty the Recycle Bin"),
    (re.compile(r"\b(?:Stop-Computer)\b|\bshutdown(?:\.exe)?\s+(?:/|-)s\b", re.I), "shut down the PC"),
    (re.compile(r"\b(?:Restart-Computer)\b|\bshutdown(?:\.exe)?\s+(?:/|-)r\b", re.I), "restart the PC"),
    (re.compile(r"\bshutdown(?:\.exe)?\s+(?:/|-)l\b|\blogoff\b", re.I), "sign you out"),
    (re.compile(r"\bFormat-Volume\b|\bformat\s+[a-z]:", re.I), "format a drive, which erases everything on it"),
    (re.compile(r"\b(?:Remove-Item|ri|del|erase|rm|rmdir|rd)\b\s+(?:-\w+\s+)*['\"]?([^'\";|&]+?)['\"]?(?:\s+-\w+.*)?(?:$|[;|&])", re.I),
     lambda m, cmd: "delete " + (_name(m.group(1)) or "some files")
     + (" and everything inside it" if re.search(r"-Recurse|\s/s\b|-r\b|-rf\b", cmd, re.I) else "")),
    (re.compile(r"\b(?:Stop-Process)\b.*?-Name\s+['\"]?([\w .-]+)", re.I), lambda m, c: f"force-close {_app(m.group(1))}"),
    (re.compile(r"\btaskkill\b.*?/im\s+['\"]?([\w.-]+)", re.I), lambda m, c: f"force-close {_app(m.group(1))}"),
    (re.compile(r"\b(?:winget|choco)\s+uninstall\s+['\"]?([\w .+-]+)|Remove-AppxPackage\b", re.I),
     lambda m, c: "uninstall " + ((m.group(1) or "").strip() or "an app")),
    (re.compile(r"\b(?:winget|choco)\s+install\s+['\"]?([\w .+-]+)", re.I), lambda m, c: f"install {m.group(1).strip()}"),
    (re.compile(r"\bpip\s+install\s+([\w .=<>-]+)", re.I), lambda m, c: f"install the Python package {m.group(1).strip()}"),
    (re.compile(r"\b(?:Move-Item|move|mv)\b\s+['\"]?([^'\";|&]+?)['\"]?\s+", re.I), lambda m, c: f"move {_name(m.group(1))}"),
    (re.compile(r"\b(?:Rename-Item|ren|rename)\b\s+['\"]?([^'\";|&]+?)['\"]?\s+", re.I), lambda m, c: f"rename {_name(m.group(1))}"),
    (re.compile(r"\b(?:Set-ItemProperty|New-ItemProperty|reg\s+(?:add|delete))\b", re.I), "change a Windows setting in the registry"),
    (re.compile(r"\bnetsh\b|\bSet-NetFirewall\w*|\bNew-NetFirewallRule\b", re.I), "change your network or firewall settings"),
]


def describe_command(command: str) -> str:
    """A system command in plain words, or an honest 'I can't describe it'."""
    cmd = re.sub(r"^\s*(?:powershell(?:\.exe)?|pwsh|cmd(?:\.exe)?)\s+(?:-\w+\s+)*(?:/c\s+)?", "", command or "", flags=re.I)
    cmd = cmd.strip().strip("'\"")
    parts = [p for p in re.split(r"\s*(?:;|&&|\|\|)\s*", cmd) if p.strip()]
    said: list[str] = []
    for part in parts:
        for pattern, phrase in _SHELL_RULES:
            m = pattern.search(part)
            if m:
                text = phrase(m, part) if callable(phrase) else phrase
                if text not in said:
                    said.append(text)
                break
        else:
            return UNREADABLE
    return " and ".join(said) if said else "run a command on your PC"


# -- each kind of action ----------------------------------------------------------------------
def describe(tool: str, args: dict) -> str:
    """'delete_file', {'path': '...report.pdf'} -> 'delete report.pdf'."""
    a = args or {}
    if tool == "run_shell_command":
        return describe_command(str(a.get("command", "")))
    if tool == "delete_file":
        return f"delete {_name(a.get('path')) or 'that file'}"
    if tool == "shutdown_pc":
        act = str(a.get("action", "shutdown")).lower().replace(" ", "_")
        what = {"restart": "restart the PC", "sign_out": "sign you out", "signout": "sign you out",
                "logoff": "sign you out"}.get(act, "shut down the PC")
        delay = int(a.get("delay_seconds") or 0)
        return what + (f" in {delay // 60} minutes" if delay >= 120 else f" in {delay} seconds" if delay else "")
    if tool == "sleep_pc":
        return "put the PC to sleep"
    if tool == "close_window":
        return f"close {a.get('app') or 'that window'}"
    if tool == "close_tab":
        where = f" in {a['app']}" if a.get("app") else ""
        if str(a.get("scope", "")).lower() in ("all", "others", "other"):
            return f"close {'the other' if 'other' in str(a.get('scope')).lower() else 'all the'} tabs{where}"
        return f"close the {a['name']} tab{where}" if a.get("name") else f"close that tab{where}"
    if tool == "install_program":
        what = a.get("label") or _name(a.get("path")) or "that program"
        return f"install {what}" + (f" from {a['publisher']}" if a.get("publisher") else "")
    if tool == "download_file":
        return f"download {a.get('save_as') or _name(str(a.get('url', '')).split('?')[0]) or 'that file'}"
    if tool == "write_file":
        return f"save {_name(a.get('path')) or 'that file'}, replacing it if it's already there"
    if tool == "run_code":
        return f"run your {a.get('project') or 'latest'} project"
    if tool == "forget_all_memories":
        return "erase everything I remember about you"
    if tool == "reset_personality":
        return "reset my personality to how it started"
    if tool == "activate_ability":
        return f"switch on the new {a.get('name') or ''} ability".replace("  ", " ")
    if tool == "remove_ability":
        return f"remove the {a.get('name') or ''} ability".replace("  ", " ")
    # anything else: a plain verb for it, with what it acts on
    verb = _VERBS.get(tool, tool.replace("_", " "))
    if tool == "set_volume" and a.get("action"):
        return {"up": "turn the volume up", "down": "turn the volume down", "mute": "mute the sound"}.get(
            str(a["action"]).lower(), "change the volume")
    target = next((str(v) for k, v in a.items() if isinstance(v, str) and v and k in
                   ("app_name", "app", "name", "site", "query", "topic", "task", "path", "what", "which", "text")), "")
    return f"{verb} {_name(target) if '\\\\' in target or '/' in target else target}".strip()[:120]


_VERBS = {"open_application": "open", "open_website": "open", "open_path": "open", "play_media": "play",
          "web_search": "search the web for", "set_volume": "change the volume", "take_screenshot": "take a screenshot",
          "look_at_screen": "look at your screen", "look_through_camera": "look through your camera",
          "lock_pc": "lock the PC", "take_note": "save a note:", "window_control": "change a window",
          "browser_task": "use the browser to", "forget_fact": "forget", "forget_topic": "forget what I learned about",
          "remove_event": "remove the calendar event", "write_code": "write code for", "update_code": "change your code:",
          "stop_code": "stop", "learn_topic": "learn about", "build_ability": "build a new ability:",
          "fix_problem": "try to fix", "apply_fix": "apply the fix", "use_phone_gps": "use your phone's GPS"}


def permission_question(description: str, critical: bool) -> str:
    """What she says when she needs your OK."""
    reason, tool, args = _parse(description)
    action = describe(tool, args) if tool else (reason or "do that")
    lead = (reason + " ") if tool and reason else ""
    ask = "Say confirm to go ahead, or no." if critical else "Yes or no?"
    note = " You can see it in my terminal." if action == UNREADABLE else ""
    return f"{lead}Are you sure you want to {action}?{note} {ask}"

"""Guards on the source itself — habits that are easy to re-introduce and hard to notice.

These assert about VERONICA's own files rather than her behaviour. Each one exists because
the mistake it catches was actually made, cost real debugging time, and would pass every
behavioural test.
"""
from __future__ import annotations

import ast
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "veronica"
SOURCES = sorted(PACKAGE.rglob("*.py"))


def _lines(pattern: re.Pattern) -> list[str]:
    hits = []
    for path in SOURCES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(PACKAGE.parent)}:{number}: {line.strip()}")
    return hits


def test_the_package_imports_without_any_source_file_being_syntactically_broken():
    """Cheap proof that nothing in the tree is unparseable, including modules no test imports."""
    broken = []
    for path in SOURCES:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            broken.append(f"{path.name}:{e.lineno}: {e.msg}")
    assert not broken, "unparseable source:\n" + "\n".join(broken)


def test_nothing_swallows_ctrl_c():
    """`except:` also catches KeyboardInterrupt, SystemExit and MemoryError.

    Seventeen of these had accumulated in the vision layer, around Windows COM property
    reads that legitimately fail per element. The handling was right; the net was too wide,
    and a long UI-tree scan could not be interrupted. `except Exception:` is the fix.
    """
    bare = _lines(re.compile(r"^\s*except\s*:\s*$"))
    assert not bare, "bare `except:` swallows Ctrl+C — use `except Exception:`:\n" + "\n".join(bare)


def test_no_behaviour_is_gated_on_the_identity_of_a_bound_method():
    """Python rebuilds a bound method on every attribute access, so `self.m is self.m` is False.

    Escalation to the cloud shipped dead for exactly this reason: the gate read
    `if engine is not self._recognize_local_whisper`, which is always true. Every test
    passed, because the tests monkeypatched that attribute into a plain function and
    identity held. Compare explicit labels instead — see docs/HEARING.md.
    """
    gated = _lines(re.compile(r"\bis(?:\s+not)?\s+self\._[a-z_]+\s*(?:[:)]|$)"))
    assert not gated, "identity test against a bound method is always false:\n" + "\n".join(gated)

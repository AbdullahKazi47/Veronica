from veronica.core.compound import split_compound_command


def test_single_clause_passthrough():
    assert split_compound_command("open chrome") == ["open chrome"]


def test_split_on_then():
    assert split_compound_command("open chrome then close notepad") == [
        "open chrome",
        "close notepad",
    ]


def test_split_on_comma_when_followed_by_verb():
    assert split_compound_command("open chrome, close notepad") == [
        "open chrome",
        "close notepad",
    ]


def test_split_on_and_when_followed_by_verb():
    out = split_compound_command("close spotify and open chrome")
    assert out == ["close spotify", "open chrome"]


def test_three_clauses_with_and():
    out = split_compound_command(
        "close Spotify, open Chrome and remind me to check email"
    )
    assert out == ["close Spotify", "open Chrome", "remind me to check email"]


def test_does_not_split_on_conjunctive_object():
    # "cats and dogs" is one thing being searched for — must not split.
    out = split_compound_command("search for cats and dogs")
    assert out == ["search for cats and dogs"]


def test_does_not_split_on_conjunctive_object_in_what_is():
    out = split_compound_command("what is the weather in London and Paris")
    assert out == ["what is the weather in London and Paris"]


def test_does_not_split_when_following_clause_lacks_verb():
    # "open Spotify, the one from yesterday" — "the one" is not a verb.
    out = split_compound_command("open Spotify, the one from yesterday")
    assert out == ["open Spotify, the one from yesterday"]


def test_ampersand_split():
    out = split_compound_command("open chrome & close notepad")
    assert out == ["open chrome", "close notepad"]


def test_turn_on_verb_wins_over_turn():
    out = split_compound_command("turn on wifi and open chrome")
    assert out == ["turn on wifi", "open chrome"]


def test_filler_stripping():
    out = split_compound_command("please open chrome and please close notepad")
    assert out == ["open chrome", "close notepad"]


def test_trailing_conjunction_drops():
    # A trailing "and" with nothing after is just dropped, not a new clause.
    out = split_compound_command("open chrome and")
    assert out == ["open chrome"]


def test_empty_input():
    assert split_compound_command("") == []
    assert split_compound_command("   ") == []


def test_semicolon_boundary():
    out = split_compound_command("open chrome; close notepad")
    assert out == ["open chrome", "close notepad"]
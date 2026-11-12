# The island: VERONICA under your camera

When she starts (from **Start VERONICA.bat** or when you sign in), her terminal hides and a
small island appears at the top centre of the screen, just under the camera.

Its look follows a "live lyrics" island for Windows (a reel by TwoCode): frosted glass
tinted with the current colour, springy motion between shapes, and words that light up as they're
sung. Here the words are hers.

| What you see | What it means |
| --- | --- |
| A small glass **orb** with a slowly turning cyan and violet core | She's waiting for her name |
| It springs open into a **glass bar** tinted blue, with flowing waves that rise with your voice | You said "Veronica" and she's listening. In conversation mode it stays open between turns |
| An **amber** tint and a shimmering "Thinking…" | She's working out the answer |
| A taller **violet** bar where her sentence lights up word by word as she speaks | She's speaking |

**Click the island** and it drops into a glass card. The card shows what you said, her whole reply,
and buttons: **Show/Hide terminal**, and **Stop talking** while she speaks (she stops and goes back
to waiting for her name). Click it again, or leave it for about 12 seconds, and it folds back.
Moving the mouse over the island lifts it slightly.

The island is always on top and has no taskbar button. Clicks next to it go through to whatever is underneath.

She is never left invisible:
- If she stops unexpectedly (an error, or you close her window), the island brings the terminal
  back so you can read what happened, then goes away.
- If the island itself stops, she restarts it up to 3 times, then shows the terminal.
- "Go to sleep" closes both quietly.

The waveform is measured against the room's own background level, so a noisy room doesn't look
like speech. It's the same meter as the line in the terminal.

## Settings (`config/config.yaml`, under `voice:`)
```yaml
island: true                  # false = no island, the terminal as before
island_hides_terminal: true   # false = keep the terminal visible as well
```

## How it works
- `veronica/ui/island.py` is a small separate program (PyQt6). It runs in its own process, so it can't
  slow down or crash her voice loop. It's sharp on scaled screens.
  The glass is painted (translucent, tinted, with a sheen). It doesn't blur the desktop behind it,
  because Windows' real blur can't follow a rounded, changing shape.
- `veronica/ui/island_link.py` is VERONICA's side. It starts the island, sends her state, the mic
  level (about 25 times a second), what you said and each sentence she starts saying, over a local
  socket (127.0.0.1 only). It listens for the island's Stop button and watches that the island is alive.
- `veronica/ui/cli.py` marks the moments: waiting for her name, listening, thinking, speaking.
- `tests/test_island.py` holds the tests.

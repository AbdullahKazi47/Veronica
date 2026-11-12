# The island: VERONICA under your camera

When she starts (from **Start VERONICA.bat** or when you sign in), her terminal hides and a
small island appears at the top centre of the screen, just under the camera.

| What you see | What it means |
| --- | --- |
| A small dark **circle** with a slow blue glow | She's waiting for her name |
| It opens into a **pill** with a blue waveform that moves with your voice | You said "Veronica" and she's listening. In conversation mode it stays open between turns |
| An **amber** shimmer | She's working out the answer |
| A **white** wave | She's speaking |

**Click the island** to show her terminal, and click again to hide it. The island is always on top
and has no taskbar button.

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
- `veronica/ui/island.py` is a small separate program (Tk). It runs in its own process, so it can't
  slow down or crash her voice loop. It's DPI-aware, so it looks sharp on scaled screens.
- `veronica/ui/island_link.py` is VERONICA's side. It starts the island, sends her state and the mic
  level about 25 times a second over a local socket (127.0.0.1 only), and watches that it's alive.
- `veronica/ui/cli.py` marks the moments: waiting for her name, listening, thinking, speaking.
- `tests/test_island.py` holds the tests.

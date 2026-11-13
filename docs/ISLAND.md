# The island: VERONICA under your camera

When she starts (from **Start VERONICA.bat** or when you sign in), her terminal hides and a
small island appears at the top centre of the screen, just under the camera.

Its look follows a "live lyrics" island for Windows (a reel by TwoCode): glass, springy motion
between shapes, and words that light up as they're sung. Here the words are hers.

It is flat black and white on purpose: no colours, no gloss. You tell the states apart by shape and motion.

| What you see | What it means |
| --- | --- |
| A small black circle: a thin white **ring** that breathes slowly, with a dot in the middle | She's waiting for her name |
| It springs open into a flat black **bar** with flowing white waves that rise with your voice (the dot swells as you speak) | You said "Veronica" and she's listening. In conversation mode it stays open between turns |
| A white arc spinning around the orb, and a shimmering "Thinking…" | She's working out the answer |
| A taller bar where her sentence lights up white, word by word, as she speaks | She's speaking |

**Click the island** and it drops into a black glass card. The card shows what you said and her whole
reply, with two round icon buttons in the corner:
- **`>_` (terminal):** shows or hides her terminal. White with a black icon means it's showing.
- **■ (stop):** only while she speaks. She stops mid-sentence and goes back to waiting for her name.

Click the card again, or leave it for about 12 seconds, and it folds back. Moving the mouse over the
island lifts it slightly, and buttons light up under the mouse.

The island is always on top and has no taskbar button. Clicks next to it go through to whatever is underneath.

She is never left invisible:
- If she stops unexpectedly (an error, or you close her window), the island brings the terminal
  back so you can read what happened, then goes away.
- If the island itself stops, she restarts it up to 3 times, then shows the terminal.
- "Go to sleep" closes both quietly.

The waveform is measured against the room's own background level, so a noisy room doesn't look
like speech. It's the same meter as the line in the terminal.

## Hover tools (idea from Fenn, fenn.click)
Move the mouse onto the globe while she's idle, and after a moment the island springs open into a
black glass grid. The tiles come in one after another. Move away and it folds back. The moment she
starts listening, thinking or speaking, the grid makes way for her.

| Tile | What it does |
| --- | --- |
| **Header** | Time and date, and your next reminder ("Next: Call mom at 6:00 PM"). Click the globe in the corner to open her conversation card |
| **Music** | What's playing in Spotify, YouTube or any player, from Windows' own media controls, with previous / play-pause / next. The little equaliser moves while music plays |
| **Awake** (cup) | Keeps the PC from sleeping or turning off the screen. White means on. It stops if the island stops |
| **Shot** (camera) | The grid folds away, then she saves a screenshot to Pictures\Screenshots and copies it so you can paste it |
| **Mic** | Mutes her: she stops answering her name and follow-ups until you press it again. The globe shows a small slash while she's muted |
| **Terminal** | Shows or hides her terminal. White means it's showing |
| **Power** | Closes VERONICA completely: island, voice and terminal. Press it once and it asks "Sure?"; press again within 3 seconds to confirm. She says goodbye and exits. It never touches the PC itself |
| **System** | Live CPU and memory (the bars glide to new values), battery and charging, network up and down, and a CPU graph |
| **My phone** (bottom row) | Shows whether your phone page is open, with a live dot. **Send clipboard** and **Send file** send to it, and you can drag files onto the island. Files from the phone land in Downloads\From phone. See docs/PHONE.md |

While the grid is closed it costs nothing. Stats are read once a second and music every 3 seconds,
and only while it's open. It's pre-drawn once off screen at start, because the first draw looked
up fonts and froze the first hover for about a second.

## Motion
- Every change is eased, never stepped. Shapes move on springs tuned just under critical, so
  they glide into place without bouncing. Colour, glow, the "Listening" label and the arc
  around the globe all fade in and out.
- The waves speed up as you talk by building up speed gradually, so a change in your volume
  never makes them jump.
- "Listening" sits beside the globe with the waves to its right. As you start talking it fades
  out and the waves take its space.
- Her line is drawn on its own layer with a soft fade at both ends. Words glide in and out of
  view and never slide over the globe. A new sentence rises in as the last one lifts away.
- Steady 60 fps, measured: no frame took more than 9.4 ms. It drops to 30 fps while idle, when
  only the slow breathing moves. CPU use is about 2% idle and 4% while listening on this laptop.

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

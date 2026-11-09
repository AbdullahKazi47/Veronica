# Daily builds: one real project a day in your GitHub repo

VERONICA picks one small, original project a day and Claude Code builds it into a **separate**
GitHub repo, your portfolio. It runs in the background whenever the laptop is on. You don't have
to tell her anything. It never touches VERONICA's own code.

## How a day goes
1. About 10 minutes after she starts, she checks whether today's project is done.
2. If not, the AI model picks an idea from your topics (Python tools, web apps, games, defensive
   cybersecurity learning). She skips anything already built and anything you asked for next.
3. Claude Code builds it headless in the project's own folder: code, README and tests.
4. **She checks the work herself** before anything is committed:
   - only that folder changed
   - there's a README and there are tests
   - the tests pass when she runs them
   - no API key, token, private key, card number, Aadhaar, PAN or SSN appears in any file
5. She commits it with a message saying it was built with Claude Code, pushes it, updates the
   repo's README table of projects, and tells you: "By the way, I built … and added it to your
   GitHub repo." If you're not around, she says it in the next greeting.

## Laptop off, crash, or Claude Code stopped
Every step is saved to `data/daily_builds.json` **before** it starts:
`picked → building → built → committed → pushed`.
- **Shutdown or crash mid-build:** next time she starts, the job is resumed. Claude Code is told
  there's an unfinished attempt in the folder and finishes it, keeping what works.
- **Work not accepted** (tests fail, no README…): she retries and tells Claude Code why. After 3
  tries (`max_attempts`) she deletes the unfinished folder and moves on. It was never committed.
- **Claude Code usage limit:** she waits an hour, and that wait doesn't count as a try.
- **No internet when pushing:** the commit is kept and pushed later.
- **"Go to sleep" while building:** the Claude Code process is stopped with her, then resumed.
- **On battery below 30 %:** she waits for the charger.

Measured live on 2026-11-09: one project (password strength checker, 4 files, 356 lines) took
about 1 minute. The first try was rejected by her own check (a false alarm, since fixed). The
next start resumed it and pushed it.

## A new Claude Code chat every time
Every build, and every retry, is a **brand-new Claude Code chat**. She never continues an
old one, so nothing piles up in one long conversation. A retry reads the folder, not the old chat.
Each chat is saved under its own name, for example "Daily build 2026-11-10: Word Counter (try 1)",
so you can open it later from Claude Code's history (`claude --resume` lists them).

## What Claude Code is allowed to do
It runs as `claude -p --permission-mode dontAsk` inside the project folder, with only these tools:
read/write/edit/search **in that folder** and `python -m pytest`. Web access, other folders,
installs and sub-agents are blocked. A live test confirmed that a read outside the folder is refused.

The idea comes only from the model and your topic list, never from your conversations, screen or
web pages. So nothing personal or planted by a website can end up in a public repo.

## By voice
| You say | She does |
| --- | --- |
| "What did you build?", "How's the daily build going?" | status: current job, last project, how many |
| "Don't do anything today", "Skip today", "No builds today", "Take the day off" | stops a build that is running right now (its work is kept, the stop isn't counted as a failed try) and does nothing more today; it carries on by itself tomorrow |
| "Pause daily builds" / "Resume daily builds" | stops until you say resume / restarts (also cancels a day off) |
| "Build a game next", "Next project should be a web app" | the next idea will be that kind |
| "Build something now" | starts the next step right away, in the background |

## Setup
In `config/config.yaml`:
```yaml
daily_builds:
  enabled: true
  repo_url: "https://github.com/<you>/<repo>.git"
```
The repo is cloned to `Documents/VERONICA Projects/<repo name>` (or `local_dir`). Pushing uses
the GitHub sign-in git already has on this PC.

## Cost
Each build is one Claude Code session on your Claude plan, roughly what a short coding chat
uses. That's about $0.4–2 of usage per project at API prices. Picking the idea is one AI-model request.

## Files
- `veronica/automation/daily_builds.py`: the job, the checks, resume logic
- `veronica/tools/daily_build_tools.py`: the voice controls
- `tests/test_daily_builds.py`: real git and pytest, fake Claude Code

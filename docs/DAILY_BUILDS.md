# Daily builds: one real project a day, each in a new GitHub repo

VERONICA picks one small, original project a day and Claude Code builds it. She then creates a
**new GitHub repo for it, named after the project** (for example `password-strength-checker`)
and pushes the code there. It runs in the background whenever the laptop is on. You don't have to
tell her anything. It never touches VERONICA's own code.

## How a day goes
1. About 10 minutes after she starts, she checks whether today's project is done.
2. If not, the AI model picks an idea from your topics (Python tools, web apps, games, defensive
   cybersecurity learning). It also picks a repo name that says what the code does. She skips
   anything already built, and any name you already have on GitHub (it becomes `name-2`).
3. Claude Code builds it headless in the project's own folder:
   `Documents/VERONICA Projects/Daily Builds/<repo-name>`. That means code, README, tests and a .gitignore.
4. **She checks the work herself** before anything is committed:
   - there's a README and there are tests
   - the tests pass when she runs them
   - no API key, token, private key, card number, Aadhaar, PAN or SSN appears in any file
5. She commits it, creates the new public repo with the project's one-line summary as its
   description, pushes, and tells you: "By the way, I built … and put it in a new GitHub repo
   called …". If you're not around, she says it in the next greeting.

She only ever creates **new** repos. She never pushes into a repo she didn't create.

## Laptop off, crash, or Claude Code stopped
Every step is saved to `data/daily_builds.json` **before** it starts:
`picked → building → built → committed → repo created → pushed`.
- **Shutdown or crash mid-build:** next time she starts, the job is resumed. A new Claude Code
  chat is told there's an unfinished attempt in the folder and finishes it, keeping what works.
- **Work not accepted** (tests fail, no README…): she retries and tells Claude Code why. After 3
  tries (`max_attempts`) she deletes the unfinished folder and moves on. Nothing reached GitHub.
- **Claude Code usage limit:** she waits an hour, and that wait doesn't count as a try.
- **No internet, or GitHub down:** the commit is kept and the repo is created and pushed later.
- **Power cut right after the repo was created:** she remembers she was creating it, so she reuses
  that repo instead of making a second one.
- **"Go to sleep" while building:** the Claude Code process is stopped with her, then resumed.
- **On battery below 30 %:** she waits for the charger.

Measured live on 2026-11-09: one project (password strength checker, 4 files, 356 lines) took
about 1 minute to build and check.

## A new Claude Code chat every time
Every build, and every retry, is a **brand-new Claude Code chat**. She never continues an
old one, so nothing piles up in one long conversation. A retry reads the folder, not the old chat.
Each chat is saved under its own name, for example "Daily build 2026-11-10: Word Counter (try 1)",
so you can open it later from Claude Code's history (`claude --resume` lists them).

## What Claude Code is allowed to do
It runs as `claude -p --permission-mode dontAsk` inside the project folder, with only these tools:
read/write/edit/search **in that folder** and `python -m pytest`. Web access, other folders,
installs, git and sub-agents are blocked. A live test confirmed that a read outside the folder is
refused. Git and GitHub are done by VERONICA's own code, after her checks.

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

## Setup (once)
1. GitHub CLI is installed (`gh`, from GitHub via winget).
2. **You** sign in once: run `gh auth login --web --git-protocol https`, then choose GitHub.com
   and confirm in the browser. VERONICA never sees your password or token. Until you sign in she
   waits, and "what did you build?" tells you she isn't signed in.
   **Sign in from your own terminal** (Windows Terminal, or the Code tab's Terminal panel), not
   from a Claude Code tool shell. Those tool shells keep their own separate saved-login store, so
   a sign-in done there is invisible to VERONICA. Found on 2026-11-10: she kept saying "not signed
   in" after a sign-in that only Claude's shells could see. To check it the way she sees it, open a
   normal `cmd` window and run `gh auth status`.
3. `config/config.yaml`:
```yaml
daily_builds:
  enabled: true
  visibility: public      # or private
```

## Cost
Each build is one Claude Code session on your Claude plan, roughly what a short coding chat
uses. That's about $0.4–2 of usage per project at API prices. Picking the idea is one AI-model request.

## Files
- `veronica/automation/daily_builds.py`: the job, the checks, the GitHub CLI wrapper, resume logic
- `veronica/tools/daily_build_tools.py`: the voice controls
- `tests/test_daily_builds.py`: real git and pytest; fake Claude Code and a fake GitHub made of local repos

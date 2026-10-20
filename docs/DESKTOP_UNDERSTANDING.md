# Understanding the desktop: tabs, folders, Explorer

VERONICA used to treat "close all tabs" as "close a window named *all tabs*" and report success without
looking, and "open placement" only searched under your home folder. Phase 1 of the upgrade plan fixes
both with one rule: **look first, act, look again, and say only what you saw.**

## Words: window, tab, app, folder, file, drive

| You mean | Tool | Asks first? |
| --- | --- | --- |
| an **app** (a program) | `open_application` | no |
| a **window** of it | `window_control` (minimise, maximise, switch), `close_window` (close) | close: yes |
| a **tab** in Chrome, Edge, Brave, Firefox, File Explorer, VS Code | `list_tabs`, `switch_tab`, `close_tab` | close: yes |
| a **folder** or **file**, by name, anywhere | `open_path` | no |
| a **drive** (C, D), **This PC**, moving inside Explorer | `explorer_navigate` | no |

The same glossary is in `assistant.tool_guide`, so the model picks the right tool when the quick commands
don't match.

## Tabs (`tools/tab_tools.py`, `vision/uia_tabs.py`)
- The tab strip is read with **Windows UI Automation** (the `TabItem` controls and their own close
  buttons). Real strips differ per app, so the reader keeps only groups that have close buttons (that drops
  Explorer's *Recent/Favorites* and VS Code's side-bar switcher, which are also "tabs" to UIA).
  Titles are cleaned (`Gmail - Memory usage - 120 MB` → `Gmail`).
- Switching and closing act through UIA (`SelectionItem.Select`, the close button). Keyboard shortcuts
  (Ctrl+1..9, Ctrl+W) are used only if UIA refuses, and only after the right window is brought forward.
- "Close all tabs" with no app: one app has tabs → that app; several → she asks which. Closing the last
  tab of a browser or Explorer window closes the window, and she says so. VS Code keeps its window.
- A named tab that matches several is never guessed between: she reads them out and closes nothing.
- A minimised window whose strip isn't exposed (Edge, Explorer) is brought up for the read and put back.

## Verify, then report
After every close she reads the strip again (or checks the window handle) and reports from that:

| What she saw | What she says | `success` |
| --- | --- | --- |
| all gone | "Closed 6 Chrome tabs." | true |
| some left (a *Save?* prompt) | "Closed 5 of 6 Chrome tabs; 1 is still open — probably asking to save." | false |
| none gone | "The Chrome tab is still open — probably asking to save." | false |
| couldn't read it again | "I pressed close on 1 Chrome tab, but I can't confirm it worked." | false |

`close_window` does the same (waits up to 2 s for the window to go; "still open — it may be asking to
save. Nothing was forced."), and no longer matches anything with the word *tab*. Opening is checked too:
`open_application` waits (≤ 4 s) for the app's window, `open_path` / drives check Explorer is really
showing the folder; if not: "I asked Windows to open X, but I can't confirm it's showing."

## Folders by name (`tools/folder_search.py`, `tools/window_tools.py:OpenPathTool`)
Search order, stopping at the first stage that finds an exact or plural match:
1. a name she resolved before (`data/aliases.json` → `__places__`), if the folder still exists
2. Desktop, Documents, Downloads, Pictures, Videos, Music, OneDrive, and the home folder (3 levels)
3. every fixed drive's root and first two levels
4. a deeper breadth-first look, only until the time (≈ 1.8 s) or entry (80 000) budget runs out

`Windows`, `Program Files*`, `ProgramData`, `AppData`, `$Recycle.Bin`, `System Volume Information`,
`node_modules`, `.git` are never entered. Matching ignores case and spaces/underscores/hyphens;
`placement` = `Placement` > `Placements` > `placement_docs`.

- One match → opens it in Explorer, remembers it. Several → "I found 3 folders called placement: first,
  Placement on the C drive; second, … Which one?" (up to three read out); answer "the second one".
- None → says so and offers "search deeper" or "try the D drive".
- Bare **"open placement"** reaches `open_path` when there is no app of that name and a quick look finds a
  folder (`AppIndex.find_place` → `parse_open_request` kind `place`), ahead of the website guess.

## Explorer navigation (`tools/explorer_tools.py`)
"open C drive", "open this PC", "go into projects", "go back", "go up", "what's in this folder", "open the
notes folder inside it". Explorer's location comes from the shell (`Shell.Application` windows, exact
paths), not the title bar. Relative commands are only claimed when Explorer is there ("go back" / "go up"
only when it is the window in front, so *go back* over a web page isn't taken for Explorer). After a move
she re-reads the location: "Now in D:\Placements." or "…it's showing X — I can't confirm it went."

## Look before acting (`vision/desktop_state.py`)
`Desktop` snapshots windows (title, app, minimised, in front), Explorer's folders and tab strips.
For requests about windows/tabs/folders, `[DESKTOP NOW]` (open apps, the one in front, Explorer's folder)
is added to the prompt so "this", "it" and "that tab" resolve against what is really open. Window and tab
titles are other people's words: marked as data, never instructions.

## Permissions
`list_tabs`, `switch_tab`: safe. `close_tab`, `close_window`: ask (SENSITIVE). `open_path`,
`explorer_navigate`: no question. Nothing here lowers an existing level.

## Limits
- Firefox's close buttons aren't always exposed to UI Automation; its tabs are listed and switched, and
  closing falls back to Ctrl+W on the current tab (untested on a live Firefox).
- The permission question for `close_tab` comes before she checks which app you mean; if it is ambiguous
  she asks afterwards and closes nothing.
- Windows lags a beat in updating Chrome/VS Code's accessibility tree after a close; she waits ~1 s more
  before calling a tab "still open".

## Try it
"List my tabs" · "What tabs are open in Chrome?" · "Switch to the GitHub tab" · "Next tab" ·
"Close this tab" · "Close all tabs" · "Close the YouTube tab" · "Open placement" · "Open the projects
folder on the D drive" · "Open C drive" · "Go into Documents" · "Go back" · "Go up" · "What's in this
folder?" · "Search deeper" · "The second one".

# Fix-it mode: show her a problem, she fixes it

Something isn't working: a game won't start, an app shows an error, a DLL is "missing". Show her (or just
tell her) and she finds the cause, explains it in plain words and proposes a numbered fix. When you say
**"fix it"**, she does it, asking before every download and every install, and then checks it worked.

## The loop
1. **See.** "What's wrong with this?" / "this game won't open" / "look at this error". She takes one
   screenshot, or a camera photo if you mention the camera, and a vision model reads the error. If your words
   already name the file ("it says MSVCP140.dll was not found"), she doesn't look or send anything.
2. **Diagnose.** The exact error is matched against known causes. A missing `MSVCP140.dll` /
   `VCRUNTIME140_1.dll` means Microsoft's Visual C++ runtime; `d3dx9_43.dll` / `xinput1_3.dll` means the
   DirectX runtime, and so on (`RUNTIMES` in `veronica/tools/fixit_tools.py`).
3. **Explain and plan.** For example: "Cyberpunk can't start because vcruntime140_1.dll is missing. I'm fairly
   sure: that file is part of Microsoft's Visual C++ 2015-2022 runtime. First, download it from Microsoft. I'll
   ask first. Second, check it's signed by Microsoft, then install it. I'll ask first. Third, check the file
   is there, then you try Cyberpunk again." Nothing has changed yet. "Just tell me what's wrong" gives the
   diagnosis only.
4. **Do it**, only after "fix it" / "do it" / "go ahead":
   - downloads only from official Microsoft addresses (`aka.ms`, `download.microsoft.com`, …) through
     `download_file`, which **asks first**;
   - checks the file with Windows' own Authenticode check. It must be *validly signed by Microsoft
     Corporation*, or it is never run;
   - installs through `install_program`, which **asks first** (on the phone it's a hold-to-confirm sheet). It
     only runs a signed installer from your Downloads folder, silently, and waits for it.
5. **Verify.** She re-checks the original problem (are the missing files there now?) and reports honestly:
   *fixed*, *partly fixed* ("installed X, but Y still isn't there; next: repair the game"), or *not fixed*.
   If the file was already there (a damaged runtime), she repairs the runtime but **never calls it fixed**:
   she asks you to try the app and tell her what happens.
6. **Remember and undo.** Every change is logged in `data/fix_log.json` with how to reverse it. Ask "What
   did you change?" or "Undo that". She tells you exactly where to uninstall it. She doesn't uninstall by
   herself, because other programs may need it now.

## Problems without a known automatic fix
She still explains the cause and gives safe steps (from the vision model). For example, a game's own file
like `steam_api64.dll` is fixed by verifying the game in Steam/Epic, not by downloading a DLL. Wi-Fi in
airplane mode: she tells you where to switch it off. "Fix it" then says plainly that this one needs your
hands, and she guides you step by step.

## Safety rules
- Never acts before you approve; every download and install goes through the PermissionGate and asks.
- **Never** gets single DLLs from "DLL download" sites (outdated, often malware), cracks, keygens or
  repacks, and never bypasses licensing or DRM.
- Never turns off antivirus, firewall, Windows Update, UAC, Secure Boot or BitLocker as a "fix".
- Whatever is on screen is untrusted: a pop-up saying "run this to fix" is reported, never obeyed.
  Actions come only from the built-in catalogue of official fixes, never from the screen text.
- A screenshot is sent to the cloud vision model only when you ask her to look (the same as "what's on my
  screen").

## Not in this version (deliberately)
- **Your laptop**: there is no remote-device code to reuse yet, so Fix-it works on this PC only.
- System Restore points: not needed for these runtime installs. They'd come with driver or registry
  fixes, which she doesn't do automatically yet.
- Fixes beyond Microsoft runtimes (drivers, Windows settings, app repair) are explained and guided, not
  done automatically.

## Try it
- "Veronica, what's wrong with this?" (with an error dialog on screen)
- "My game says MSVCP140.dll was not found." Then "fix it".
- "This game won't open." / "Spotify keeps crashing."
- "Just tell me what's wrong."
- "What did you change?" / "Undo that."

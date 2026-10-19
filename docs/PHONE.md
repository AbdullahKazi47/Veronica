# Talking to VERONICA from your phone

`veronica/ui/remote/` serves a small page (chat, hold-to-talk mic, a PC menu) that your own devices
can open. Everything you ask goes through the same brain and the same permission gate as at the PC.

**Address:** `https://pc.tailecf138.ts.net` (Tailscale, your devices only). The first time, open the link
in `data/remote_link.txt` on the PC (it carries the access token); the phone remembers it.

## How it stays safe
- The server listens on `127.0.0.1` only. `tailscale serve --bg 7272` (run once on the PC) publishes it to
  *your* Tailscale devices over HTTPS. Nothing is open to Wi-Fi or the internet.
- Every API call needs the token (`data/remote_token.txt`), compared in constant time, with a lockout.
- A permission question raised by a phone request is asked **on the phone**, never auto-approved; no
  answer in 60 s counts as no.

## The permission sheet (mobile)
A question rises as a bottom sheet over the chat, so it can't scroll away, with:
- what it means in plain words ("Shut down your PC? It turns off in 30 seconds. Your phone can't switch it
  back on"), the file or command involved, and the raw call under "Technical details";
- a countdown ("Counts as No in 46 s"); *Not now* has the focus;
- red for the ones that can't be undone or take her offline (shut down, restart, sign out, delete, shell),
  amber for the rest (sleep, close a window, run code, save/download a file);
- **hold the button for a second** to confirm the red ones (a tap does nothing); the amber ones take a tap;
- after a shutdown or restart is allowed, a **Cancel** card counts down the 30 s she waits;
- it follows the phone's light or dark mode, respects the notch and home bar, and comes back by itself if
  you leave the app or reload while a question is waiting.

The power button in the header opens the PC menu: **Lock** (no question), **Sleep, Sign out, Restart,
Shut down** (each asks first).

## Reaching the PC after you sign out
Signing out ends your Windows session, and Tailscale on Windows disconnects with it unless it runs
unattended: in the Tailscale tray menu choose **Preferences > Run unattended**. VERONICA herself can't run
while signed out (no desktop, no mic); lock with Win+L instead and she keeps running. With Remote Desktop on
(`scripts/remote-access-setup.ps1`) you can sign back in from the phone.

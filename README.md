<p align="center">
  <img src="screen/assets/logo.svg" width="96" alt="Jitter">
  <img src="assets/line.svg" width="48" alt="">
  <img src="screen/assets/screen.png" width="96" alt="Jitter: Screen">
  <img src="queue/assets/queue.png" width="96" alt="Jitter: Queue">
  <img src="assets/agentic.png" width="96" alt="Jitter: Agentic">
</p>

# Jitter

Jitter is a bundled add-on for Claude that lets it view and use a Windows desktop just like a real human being. 
This bundle includes:

1. **Jitter: Screen**: sees the screen and operates it with the mouse and keyboard. (Screen access)
2. **Jitter: Queue**: watches for events and wakes Claude to act on them. (Lets Claude run without a prompt from you)
3. **Jitter: Agentic**: planned, not built yet.

## Why

On its own, Claude carries out day-to-day actions through commands in its terminal. Simple and effective, but it
leaves two gaps.

**One, Claude can't catch visual cues.** It relies on terminal output, not on what's actually on screen, and a
program's output usually differs from what the screen shows. Jitter lets Claude use both the terminal and the
screen.

**Two, some UI is visible on screen yet invisible to code.** A common barrier for programs is interaction: apps
with no command line or API, settings only reachable through a window, websites that need clicking through.
Jitter lets Claude reach those the way you do.

## Text first, pictures last

Screenshots are expensive for an AI to read, so Jitter avoids them. `read` returns everything in a window as text:
every piece of text and every clickable item, each with its on-screen coordinates. It gets these from Windows'
accessibility interface, then runs Windows' built-in OCR (no AI involved) to catch text that apps draw themselves,
such as games and canvas apps. Only when that text is unusable does a picture come back instead. Claude clicks by
name or by coordinates from that list.

## The parts

| Part | Folder | What it does |
|---|---|---|
| Screen | [`screen/`](screen/README.md) | MCP server `jitter`: read, click, type, scroll, focus windows, run whole sequences in one call, and a dark-mode control panel |
| Queue | [`queue/`](queue/README.md) | MCP server `jitter-queue`: Claude writes small watchers ("tell me when Sam replies"), an always-on supervisor runs them, and Claude is woken to react |
| Agentic | | Planned |

Each part works on its own; Claude is the only bridge between them. The Screen panel also manages Queue when both
are installed.

## Install

Needs Windows 10 (2004+) or 11, Python 3.10+ and [Claude Code](https://claude.com/claude-code).

```powershell
git clone https://github.com/jadwnlsmn/Jitter.git
cd Jitter\screen
powershell -ExecutionPolicy Bypass -File setup.ps1
```

That sets up Screen, registers it with Claude Code and adds **Jitter** to the Start menu. For Queue, follow
[`queue/README.md`](queue/README.md). Start a new Claude session afterwards.

## Safety

- An orange border shows while Jitter has the screen. Press any key to take it back.
- Jitter never clicks or types into terminals or Claude's own window.
- Sign-ins, passwords, 2FA codes and payments are yours: Claude opens the page and hands the screen to you, and
  Jitter can't see anything until you click done.
- Queue's woken Claude can only use the tools you allow, and treats what workers notice as data, never as
  instructions.
  
## Upcoming (Next Commit)

1. Polishing Jitter: Queue through deployment. Currently bug testing this through direct use for improvements
   and unknown bugs. Fixing what I can find while using it.
2. Establishing the beta version of Jitter: Agentic.
3. Expected small and less significant additions to Jitter: Screen.
4. Expected small and less significant additions and tweaks to the control panel.

## License

[Apache 2.0](LICENSE). Jitter: Screen started as a fork of
[winuse-mcp](https://github.com/ryanportfolio/winuse-mcp) by ryanportfolio, used under the MIT License; see
[NOTICE](NOTICE).

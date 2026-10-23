<p align="center"><img src="assets/screen.png" width="96" alt="Jitter: Screen"></p>

# Jitter: Screen

Jitter lets an AI see and operate a Windows desktop the way a person does: screenshots, mouse and keyboard, plus
fast access to what's on screen through Windows UI Automation. It is an MCP server, so any MCP client can use it.
Claude Code is the first. It is one part of **Daffy**, a personal assistant project.

## Install

Needs Windows 10 (2004+) or 11 and Python 3.10+ (developed on 3.14).

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

This creates `.venv`, installs dependencies, registers the `jitter` MCP server with Claude Code (user scope), and adds
a **Jitter** entry to the Start menu. Start a new Claude session afterwards.

## The panel

Open **Jitter** from the Start menu, or ask Claude to open the Jitter panel.

- **Access**: switches for what Claude may do with Jitter. They edit `~/.claude/settings.json` for you (merged, with
  a one-time backup) and apply to new sessions.
- **Behavior**: how long Jitter holds the screen, the orange border, and windows Jitter must stay out of.
- **Immerse**: maps your open apps and Chrome tabs into address books, so Claude can act without exploring first.
- **Library**: saved routes and address books, as a searchable tree.

## What Claude can do with it

| Tool | Does |
|---|---|
| `screenshot`, `zoom`, `record`, `wait_for_change` | See the screen |
| `scan` | Scroll through a list or grid and get every view back as one numbered sheet, to find things by sight |
| `read` | A window's text in one fast call; clickable items tagged. `read(window=...)` reads another window or tab in the background, without taking focus. Every piece of text and clickable item comes with its `@x,y`; Windows' built-in OCR adds text that apps draw themselves (games, canvas apps), and only when that text is unusable does a picture come back instead of it. `E(name)` falls back to OCR too |
| `click`, `move`, `drag`, `type`, `key`, `scroll`, `wait` | Act |
| `windows`, `focus`, `click_element` | Windows, and clicking by accessible name |
| `run` | A whole sequence in one call (below) |
| `routes`, `save_route` | Saved sequences and address books |
| `journal` | Recent calls with timings and outcomes (ok, detour, error, permission), for reviewing a task |
| `human` | Hand the screen to you for a sign-in, password, 2FA or payment step; Jitter can't see the screen until you click the badge |
| `packages`, `deliver` | Your trusted login packages (name and site only); fill one into a sign-in form after you approve it |
| `done`, `panel` | Give the screen back; open the panel |

### Command language

`run("F(Messenger)E(Group chat: $1)E(Write to $1)T($2)K(enter)S")`

| Code | Action |
|---|---|
| `F(title)` | Focus a window: by title, browser tab, same words, or program name; launches the app if it isn't open. `F(a\|b)` tries alternatives |
| `G(url)` | Go to a URL in the focused browser |
| `E(name)` | Click an element by accessible name: exact, partial, scrolled into view, or same words; ties go to the whole word on something clickable, and never to an opposite like 'Remove from ...' |
| `C(x,y)` | Click at screenshot coordinates |
| `T(text)` | Type text |
| `K(keys)` | Press keys, space-separated: `K(ctrl+a backspace)` |
| `W(s)` | Wait seconds |
| `V(text)` | Stop unless `text` is on screen |
| `D(down 3)` / `D(up 5,x,y)` | Scroll |
| `Z(x1,y1,x2,y2)` | Zoom into a region |
| `S` / `X` / `X(title)` | Screenshot / read the window's text / read another window or tab in the background |
| `H(target)` / `M(target)` / `P(from > to)` | Hover / right-click / drag; a target is a name or `x,y`, and `name#2` picks the second of several |
| `U(reason)` | Hand the screen to you for a step only you should do (see `human`) |
| `L(package)` | Fill the sign-in form with a login package, once you approve (see `deliver`) |
| `R(app/task, args...)` | Run a saved route; `$1`, `$2` in the route take the args |

Escape `( ) , \` inside values with a backslash. Only a permission restriction stops a task. Any other failed step returns a detour (what failed, the window's text with coordinates, the remaining steps; a picture only when the text is unusable) so Claude can solve that step another way and carry on.

## Safety

- An orange border shows while Jitter has the screen. Press any key to take it back: Jitter refuses its next action.
- Jitter never clicks or types into terminals or Claude's own window. This is fixed, not a setting.
- Sign-ins and passwords are yours. Claude opens the page and hands over with `human`: the border goes away, the badge says what's needed, your typing doesn't stop Jitter, and nothing on screen is captured or read until you click the badge.
- Login packages (panel, Behavior > Login packages) are logins you choose to let Jitter fill in. Each starts untrusted; its switch lets Claude see its name and site, never its contents. Every delivery pauses Jitter and shows a box in the middle of the screen, and only your own mouse click on Approve fills the form (Jitter's clicks can't). It's refused when the page's address doesn't match the package's site. Stored in `packages.json`, encrypted with Windows DPAPI for your account. That keeps it unreadable on other accounts and machines, but not from programs running as you.
- PyAutoGUI's fail-safe stays on: move the mouse to the top-left corner to abort.

## Files

| Path | What |
|---|---|
| `server.py` | The MCP server |
| `panel.py`, `panel.html` | The control panel |
| `setup.ps1` | Installer |
| `settings.json`, `routes/`, `addresses/` | Your data, created as you use Jitter (not in git) |

Tests: `.venv\Scripts\python -m pytest`

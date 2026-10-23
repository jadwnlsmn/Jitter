import threading
import time

import pytest
import server

REAL_COUNTDOWN = server._countdown


@pytest.fixture(autouse=True)
def no_countdown(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_countdown", lambda: None)  # tests act at once; the countdown has its own test
    monkeypatch.setattr(server, "JOURNAL", tmp_path / "journal.jsonl")  # keep test calls out of the real journal


def test_countdown_pauses_and_a_keypress_cancels(monkeypatch):
    monkeypatch.setattr(server, "settings", lambda: {**server.DEFAULTS, "countdown_seconds": 0.3})
    server._busy["paused"] = True
    threading.Timer(0.4, lambda: server._busy.update(paused=False)).start()  # user clicks the badge again
    t = time.monotonic()
    REAL_COUNTDOWN()
    assert time.monotonic() - t >= 0.65 and server._busy["countdown"] == 0  # froze while paused, then ran out
    threading.Timer(0.1, server._abort.set).start()  # user presses a key mid-countdown
    with pytest.raises(server.UserStop):
        REAL_COUNTDOWN()
    assert server._busy["countdown"] == 0 and not server._abort.is_set()


def test_native_clamps_off_failsafe_corner():
    assert min(server._native(0, 0)) >= 1


def test_blocked_titles_refuse(monkeypatch):
    monkeypatch.setattr(server.user32, "GetAncestor", lambda h, f: h)
    monkeypatch.setattr(server.user32, "GetWindowTextW", lambda h, buf, n: setattr(buf, "value", "Windows PowerShell"))
    monkeypatch.setattr(server, "_exe", lambda h: "powershell")  # not a browser
    with pytest.raises(PermissionError):
        server._check(1)


def test_title_block_is_whole_word_not_substring():
    # the "claude" rule must catch the Claude app but not an app merely containing the letters
    assert server._title_blocked("Claude")
    assert server._title_blocked("Claude - New Chat")
    assert not server._title_blocked("Claudeswap")
    assert not server._title_blocked("Claudeswap - Trade")
    assert server._title_blocked("Windows PowerShell") and server._title_blocked("cmd.exe")


def test_browser_window_never_blocked_by_its_tab(monkeypatch):
    # a browser's title is just the active tab; the whole window must not be judged off-limits by it
    monkeypatch.setattr(server, "_title", lambda h: "Claude | Anthropic - Google Chrome")
    monkeypatch.setattr(server, "_exe", lambda h: "chrome")
    assert not server._blocked(1)
    monkeypatch.setattr(server, "_exe", lambda h: "notepad")  # a real non-browser with that title would block
    assert server._blocked(1)


def test_parse_and_escapes():
    assert server.parse("F(Personal) T(a\\) b, c)\nK(enter)S") == [
        ("F", "Personal"), ("T", "a\\) b, c"), ("K", "enter"), ("S", "")]
    assert server._args("app/x, a\\,b , c\\)") == ["app/x", "a,b", "c)"]
    for bad in ("S(1)", "T", "x", "Q(1)", "T(unclosed"):
        with pytest.raises(ValueError):
            server.parse(bad)


def test_run_detours_on_failure_and_stops_only_for_permission(monkeypatch):
    with pytest.raises(ValueError):  # parse error before any step runs
        server.run("W(0.1)Q(1)")
    monkeypatch.setattr(server, "_matches", lambda name, timeout=3.0: [])
    log = server.run("W(0.1)V(gone\\, really)W(0.2)S")[0]
    assert log.startswith("W(0.1): waited") and "FAILED" in log.splitlines()[1]
    assert "DETOUR" in log and "Remaining (from the failed step): V(gone\\, really)W(0.2)S" in log
    monkeypatch.setattr(server, "focus", lambda t: (_ for _ in ()).throw(PermissionError("off limits")))
    log = server.run("F(terminal)W(0.1)")[0]
    assert "STOPPED by a permission restriction" in log and "DETOUR" not in log
    server._wait_orig = server.wait
    try:
        server.wait = lambda s: server._abort.set() or "waited"  # user presses a key during step 1
        out = server.run("W(1)W(1)")
        lines = out[0].splitlines()
        assert lines[0] == "W(1): waited" and lines[1].startswith("W(1): STOPPED, UserStop: user pressed a key")
        assert len(out) == 1  # text first: a stopped run sends no picture
    finally:
        server.wait = server._wait_orig
    assert not server._abort.is_set() and not server._holding()  # refused once, screen released


def test_keypress_between_calls_refuses_next_action_once(monkeypatch):
    monkeypatch.setattr(server, "_blocked", lambda h: False)
    server._busy["last"] = 0.0
    server._on_user_key()  # Jitter idle: ignored
    assert not server._abort.is_set()
    server._busy["last"] = server.time.monotonic()  # Jitter acted a moment ago, Claude is thinking
    monkeypatch.setattr(server, "_blocked", lambda h: True)
    server._on_user_key()  # typing into Claude's own window: ignored
    assert not server._abort.is_set()
    monkeypatch.setattr(server, "_blocked", lambda h: False)
    server._on_user_key()  # typing into another app: user takes the screen
    assert server._abort.is_set() and not server._holding()
    with pytest.raises(server.UserStop):
        server.move(5, 5)
    with server._session():  # next call goes through
        pass
    assert server._holding()
    server.done()
    assert not server._holding()


def test_detour_inside_route_lists_route_rest_then_outer_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROUTES", tmp_path)
    monkeypatch.setattr(server, "_matches", lambda name, timeout=3.0: [])
    server.save_route("demo/inner", "W(0.1)V($1)W(0.1)")
    log = server.run("R(demo/inner, missing)S")[0]
    assert "Remaining (from the failed step): V(missing)W(0.1)S" in log


def test_words_in_ignores_case_order_and_punctuation():
    assert server._words_in("chat 4242", "4242³ | Group Chat")
    assert server._words_in("send", "Send now")
    assert not server._words_in("send", "Settings") and not server._words_in("", "anything")


def test_innermost_drops_wrappers_keeps_distinct():
    class R:
        def __init__(self, *r):
            self.BoundingRectangle = type("Rect", (), dict(zip(("left", "top", "right", "bottom"), r)))

    window, item, link, twin, other = R(0, 0, 100, 100), R(10, 10, 50, 20), R(12, 12, 40, 18), R(12, 12, 40, 18), R(10, 60, 50, 70)
    hidden = R(0, 0, 0, 0)  # display:none content reports an empty box at the corner
    kept = [c for c, _ in server._innermost([(c, None) for c in (window, item, link, twin, other, hidden)])]
    assert kept == [twin, other]


def test_routes_save_expand_and_nest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROUTES", tmp_path)
    server.save_route("demo/inner", "W($1)")
    server.save_route("demo/outer", "R(demo/inner, $1)W(0.1)")
    assert server.expand("demo/inner", ["a,b)"]) == "W(a\\,b\\))"
    log = server.run("R(demo/outer, 0.1)")[0]
    assert log.count("waited") == 2 and "STOPPED" not in log
    assert "demo/inner: W($1)" in server.routes()
    for bad in ("../x", "demo", "a/b/c"):
        with pytest.raises(ValueError):
            server.save_route(bad, "S")
    assert "needs argument $1" in server.run("R(demo/inner)")[0]


def test_record_and_wait_for_change_return_images():
    assert len(server.record(0.5, 2)) == 2
    out = server.wait_for_change(timeout=0.5, sensitivity=10**9)
    assert out[0].startswith("timed out") and out[1].data[:2] == b"\xff\xd8"


def test_read_returns_window_text():
    assert server.parse("SX") == [("S", ""), ("X", "")]
    text = server.read(max_chars=500)
    assert text.startswith("window: ") and len(text) <= 540


def test_settings_clamp_and_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    assert server.settings() == server.DEFAULTS
    cfg = server.save_settings({"blocked_titles": [" Discord ", "", "discord"], "opacity": 5, "border_px": 0, "hold_seconds": 60})
    assert cfg["blocked_titles"] == ["discord"] and cfg["opacity"] == 1.0 and cfg["border_px"] == 1
    assert server.settings()["hold_seconds"] == 60  # reloaded from disk
    monkeypatch.setattr(server, "_title", lambda h: "Discord - Friends")
    assert server._blocked(1)
    monkeypatch.setattr(server, "_title", lambda h: "Windows PowerShell")
    server.save_settings({"blocked_titles": []})
    assert server._blocked(1)  # the built-in list can't be edited away


def test_immerse_helpers():
    assert server._slug("facebook.com-messages") == "facebook-com-messages" and server._slug("??") == "unnamed"
    usage = server._usage()
    # blocked apps (whole-word: the Claude app, terminals) are excluded; "Claudeswap" is not blocked
    assert all(isinstance(h, float) and not server._title_blocked(a) for h, a in usage)


def test_claude_permissions_merge_and_backup(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    f.write_text('{"theme": "dark", "permissions": {"allow": ["Bash(git *)"]}}', encoding="utf-8")
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", f)
    server.set_permission("allow", "mcp__jitter", True)
    server.set_permission("auto", "rule A", True)
    cfg = server.load_claude()
    assert cfg["theme"] == "dark" and cfg["permissions"]["allow"] == ["Bash(git *)", "mcp__jitter"]
    assert cfg["autoMode"]["allow"] == ["$defaults", "rule A"]  # built-in safety rules kept
    server.set_permission("auto", "rule A", False)
    server.set_permission("allow", "mcp__jitter", False)
    assert server.load_claude() == {"theme": "dark", "permissions": {"allow": ["Bash(git *)"]}}
    assert "Bash(git *)" in (tmp_path / "settings.json.jitter-backup").read_text(encoding="utf-8")


def test_unicode_input_matches_windows_layout():
    assert server.ctypes.sizeof(server._INPUT) == 40  # SendInput silently ignores a mis-sized INPUT on x64


def test_narrow_prefers_whole_word_on_something_clickable():
    class C:
        def __init__(self, name, ctype):
            self.Name, self.ControlType = name, ctype

    gift, picker, label = C("Send a gift", 50000), C("Open GIF picker", 50000), C("Send GIF", 50020)
    assert server._narrow([(gift, 0), (picker, 0), (label, 0)], "GIF") == [(picker, 0)]
    a, b = C("Save", 50000), C("Save", 50000)
    assert len(server._narrow([(a, 0), (b, 0)], "Save")) == 2  # a real tie stays a tie


def test_scroll_and_zoom_commands_parse():
    assert server.parse("D(down 3)D(up 5,900,600)Z(1,2,30,40)") == [
        ("D", "down 3"), ("D", "up 5,900,600"), ("Z", "1,2,30,40")]


def test_contact_sheet_numbers_views_in_a_grid():
    from PIL import Image
    views = [Image.new("RGB", (400, 300), c) for c in ("red", "green", "blue", "white")]
    sheet = server._contact_sheet(views)
    assert sheet.width <= 1800 and sheet.height > 300  # 3 across, so 4 views take 2 rows
    assert server._same_view(views[0], views[0]) and not server._same_view(views[0], views[2])


def test_loose_matches_never_do_the_opposite():
    class C:
        def __init__(self, name):
            self.Name = name

    fav, remove, add = C("Favorites"), C("Remove from Favorites"), C("Add to Favorites")
    assert server._not_opposite([(remove, 0), (add, 0)], "Favorites") == [(add, 0)]
    assert server._not_opposite([(remove, 0)], "Remove from Favorites") == [(remove, 0)]  # asked for it
    assert server._not_opposite([(C("Log out"), 0)], "Log") == []
    assert server._not_opposite([(fav, 0)], "Favorites") == [(fav, 0)]


def test_stuck_ignores_animation_but_sees_scrolling():
    from PIL import Image, ImageDraw
    def view(offset, tile_color):
        im = Image.new("RGB", (320, 800), "#111")
        d = ImageDraw.Draw(im)
        for y in range(-400, 1200, 200):  # tiles with dark gaps, like a gif grid
            d.rectangle([10, y - offset + 10, 310, y - offset + 170], fill=tile_color)
        d.rectangle([140, 100 - offset, 180, 140 - offset], fill="white")  # one still marker that scrolls along
        return im
    before, again = view(0, "#888"), view(0, "#ccc")  # tiles animate in place between the two looks
    assert server._stuck(before, again, view(0, "#999"))  # scroll did nothing; only animation changed
    assert not server._stuck(before, again, view(330, "#888"))  # content moved
    assert not server._stuck(before, before, view(700, "#888"))  # moved almost a whole view


def test_journal_records_calls_nesting_and_outcomes(monkeypatch):
    server.run("W(0.1)W(0.1)")
    monkeypatch.setattr(server, "_matches", lambda name, timeout=3.0: [])
    server.run("V(nothing here)")
    text = server.journal()
    assert "run(W(0.1)W(0.1))" in text and "    wait(0.1)" in text  # steps nested under the run
    assert "DETOUR" in text and "2 calls" in text and "1 detours" in text


def test_tools_keep_their_parameters_through_the_journal():
    import asyncio
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "window" in tools["read"].inputSchema["properties"]
    assert set(tools["click"].inputSchema["properties"]) >= {"x", "y", "button", "clicks"}
    assert "journal" in tools and "scan" in tools


def test_x_reads_front_or_named_window():
    assert server.parse("XX(Discord|Messenger)S") == [("X", ""), ("X", "Discord|Messenger"), ("S", "")]


def test_paths_keep_their_backslashes():
    assert server._unescape(r"C:\Users\me\file.txt") == r"C:\Users\me\file.txt"
    assert server._unescape(r"a\,b \(c\) d\e") == r"a,b (c) d\e"


def test_hover_rightclick_drag_commands_parse():
    assert server.parse("H(user 2)M(377,293)P(A > B)P(334,214 > 518,214)") == [
        ("H", "user 2"), ("M", "377,293"), ("P", "A > B"), ("P", "334,214 > 518,214")]


def test_control_states_read_like_a_person_sees_them():
    class E:
        def __init__(self, v):
            self.v = v

        def GetCachedPropertyValue(self, pid):
            return self.v[pid]

    assert server._state(E({30086: 1}), 50002) == "☑" and server._state(E({30086: 0}), 50002) == "☐"
    assert server._state(E({30079: True}), 50013) == "◉"
    assert server._state(E({30019: False, 30045: "Option 2"}), 50003) == "Option 2"
    assert server._state(E({30019: True, 30045: "hunter2"}), 50004) == "(hidden)"  # never show passwords


def test_human_step_hands_over_and_hides_the_screen(monkeypatch):
    import threading
    seen = {}

    def user_signs_in():
        time.sleep(0.3)
        seen["busy"] = server._busy["human"]
        for peek in (server.screenshot, lambda: server.read(), lambda: server.zoom(0, 0, 10, 10)):
            try:
                peek()
                seen["leak"] = True
            except PermissionError:
                pass
        monkeypatch.setattr(server, "_blocked", lambda h: False)
        server._on_user_key()  # typing a password must not count as taking the screen back
        seen["aborted"] = server._abort.is_set()
        server._busy["human"] = None  # the user clicks the badge: done

    focused = []
    monkeypatch.setattr(server, "_focus_sign_in_field", lambda: focused.append(True))
    threading.Thread(target=user_signs_in).start()
    out = server.human("Human login required", wait_minutes=0.1)
    assert focused == [True]  # the cursor was put in the form before handing over
    assert seen == {"busy": "Human login required", "aborted": False}  # no "leak" key: nothing was captured
    assert out.startswith("the user is done") and server._busy["human"] is None
    assert server.parse("U(Human login required)") == [("U", "Human login required")]
    server.done()


def test_login_package_is_delivered_only_after_approval(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(server, "PACKAGES", tmp_path / "packages.json")
    monkeypatch.setattr(server, "_grant", {"key": None, "until": 0.0})
    listed = server.add_package("GitHub", "https://www.github.com/login", "octocat", "hunter2")
    assert listed == [{"name": "GitHub", "site": "github.com", "trusted": False}]  # starts untrusted, no login
    assert b"hunter2" not in server.PACKAGES.read_bytes() and b"octocat" not in server.PACKAGES.read_bytes()
    assert server.packages().startswith("no trusted")  # Claude doesn't even see an untrusted package
    with pytest.raises(PermissionError):
        server.deliver("GitHub")
    server.trust_package("GitHub", True)
    assert server.packages() == "GitHub: github.com"

    class Field:
        def SetFocus(self):
            typed.append("|")
    typed = []
    monkeypatch.setattr(server, "_guard_focus", lambda: None)
    monkeypatch.setattr(server, "_exe", lambda h: "chrome")
    monkeypatch.setattr(server, "_sign_in_fields", lambda: (Field(), Field()))
    monkeypatch.setattr(server, "_type_unicode", typed.append)
    monkeypatch.setattr(server.pyautogui, "hotkey", lambda *k: None)
    monkeypatch.setattr(server, "_chrome_url", lambda h: "evil-github.com/login")
    with pytest.raises(PermissionError):  # wrong site: refused before asking
        server.deliver("GitHub")

    def user_answers(yes):
        while server._busy["ask"] is None:
            time.sleep(0.02)
        assert typed == []  # nothing filled while the box waits
        server._busy["ask"]["answer"] = yes
    monkeypatch.setattr(server, "_chrome_url", lambda h: "github.com/login")
    threading.Thread(target=user_answers, args=(False,)).start()
    with pytest.raises(PermissionError):
        server.deliver("github")
    threading.Thread(target=user_answers, args=(True,)).start()
    out = server.deliver("github")
    assert typed == ["|", "octocat", "|", "hunter2"] and "hunter2" not in out and "octocat" not in out
    assert server.deliver("GitHub").startswith("filled")  # the password page of a two-step sign-in: no second ask
    assert "hunter2" not in server.JOURNAL.read_text(encoding="utf-8")
    assert server.parse("L(GitHub)") == [("L", "GitHub")]
    server.done()


def test_only_a_real_click_in_the_box_counts():
    rect = (100, 100, 300, 200)
    server._real_click[:] = [time.monotonic(), 150, 150]
    assert server._human_clicked(rect)
    server._real_click[:] = [time.monotonic(), 50, 150]
    assert not server._human_clicked(rect)  # a real click somewhere else
    server._real_click[:] = [time.monotonic() - 5, 150, 150]
    assert not server._human_clicked(rect)  # an old click, then Jitter's injected one


def test_keyboard_and_mouse_hooks_install():
    # a truncated module handle once meant no hooks at all: no keypress stop, no real-click approval
    import threading, time
    threading.Thread(target=server._keyhook, daemon=True).start()
    time.sleep(0.5)
    assert server._busy.get("hooks") is True


def test_a_browser_tab_does_not_block_the_window(monkeypatch):
    # a browser showing any tab (even one whose title matches a rule) is never off-limits; the tab is not the window
    monkeypatch.setattr(server, "_exe", lambda h: "chrome")
    for title in ("Claude - Google Chrome", "cmd.exe tutorial - Google Chrome", "Messenger - Google Chrome"):
        monkeypatch.setattr(server, "_title", lambda h, t=title: t)
        assert not server._blocked(1)


def test_launch_whole_word_blocks_claude_not_claudeswap(monkeypatch):
    monkeypatch.setattr(server, "_start_apps", lambda: [{"Name": "Claudeswap", "AppID": "x"},
                                                        {"Name": "Claude", "AppID": "y"}])
    monkeypatch.setattr(server, "_visible_windows", lambda: [])
    with pytest.raises(PermissionError):      # the real Claude app is still blocked from launching
        server._launch("Claude")
    # Claudeswap is allowed: it gets past the block and tries to launch (no window appears in the test -> None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(server.time, "monotonic", lambda: 1e18)  # skip the wait-for-window loop
    assert server._launch("Claudeswap") is None


def test_ocr_find_picks_one_item_and_never_the_opposite():
    items = [("C New chat", 470, 134), ("Remove from Favorites", 300, 50), ("Favorites", 300, 90), ("Share", 900, 95),
             ("Share link", 900, 400)]
    assert server._ocr_find("new chat", items) == ("C New chat", 470, 134)  # an icon glyph before the text is fine
    assert server._ocr_find("Favorites", items) == ("Favorites", 300, 90)  # never "Remove from Favorites"
    assert server._ocr_find("share", items) == ("Share", 900, 95)  # exact text beats a longer match
    with pytest.raises(LookupError):
        server._ocr_find("Settings", items)


def test_messy_ocr_means_send_a_picture():
    clean = [("Start game", 400, 300), ("Options", 400, 350), ("Quit", 400, 400)]
    noisy = [("Start game", 400, 300), ("~|", 10, 10), ("-5", 20, 20), ("(W", 30, 30), ("=_.", 40, 40)]
    assert not server._messy(clean)
    assert server._messy(noisy) and server._messy([])


def test_messy_ocr_sends_the_picture_instead_of_the_text(monkeypatch):
    from PIL import Image as PILImage
    from unittest.mock import MagicMock
    ua = MagicMock()  # a window that exposes nothing to accessibility, like a game
    ua.ElementFromHandle.return_value.FindFirst.return_value.FindAllBuildCache.return_value.Length = 0
    monkeypatch.setattr(server._AutomationClient, "instance", lambda: MagicMock(IUIAutomation=ua))
    monkeypatch.setattr(server, "_ocr", lambda h: ([("~|", 1, 1), ("-5", 2, 2)], PILImage.new("RGB", (40, 30))))
    text, picture = server._read()
    assert picture is not None and "~|" not in text and len(text.splitlines()) == 2  # one or the other, never both

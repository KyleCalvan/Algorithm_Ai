"""
app_control.py — Read from and control any desktop application.

Supported actions:
  focus          — Bring a named app window to the foreground
  type           — Type text into the currently focused app (or focus first)
  type_line      — Type text and press Enter
  read_selection — Copy selected text from the active app and return it
  read_all       — Select all + copy in the active app, return the text
  vscode_command — Run any VS Code command palette command
  vscode_type    — Type into the VS Code editor (focuses editor first)
  vscode_open    — Open a file in VS Code
  vscode_read    — Read the current VS Code editor content
  vscode_run     — Run the current file / active terminal command
  vscode_terminal— Type a command into the VS Code integrated terminal
  vscode_select_all_read — Select all editor text and return it
  hotkey         — Send a hotkey to the focused or named app
  find_window    — Return a list of open window titles matching a query
  app_type       — Focus an app by name then type into it
  clear_and_type — Select all + delete + type fresh content
  paste          — Paste text into the focused app via clipboard
"""

from __future__ import annotations

import platform
import subprocess
import time
import threading

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# ── optional imports ──────────────────────────────────────────────────────────
try:
    import pyautogui
    pyautogui.FAILSAFE   = False
    pyautogui.PAUSE      = 0.05
    _HAS_PYAUTOGUI = True
except ImportError:
    _HAS_PYAUTOGUI = False

try:
    import pyperclip
    _HAS_CLIP = True
except ImportError:
    _HAS_CLIP = False

try:
    import pygetwindow as gw
    _HAS_GW = True
except ImportError:
    _HAS_GW = False

# Windows-only UI Automation
_HAS_UIAUTO = False
if _OS == "Windows":
    try:
        import uiautomation as auto  # type: ignore
        _HAS_UIAUTO = True
    except ImportError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clipboard_get() -> str:
    """Read the system clipboard."""
    if _HAS_CLIP:
        try:
            return pyperclip.paste() or ""
        except Exception:
            pass
    if _OS == "Windows":
        try:
            r = subprocess.run(["powershell", "-Command", "Get-Clipboard"],
                               capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        except Exception:
            pass
    if _OS == "Darwin":
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            return r.stdout
        except Exception:
            pass
    if _OS == "Linux":
        for tool in [["xclip", "-selection", "clipboard", "-o"],
                     ["xdotool", "getclipboard"]]:
            try:
                r = subprocess.run(tool, capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    return r.stdout
            except Exception:
                pass
    return ""


def _clipboard_set(text: str):
    """Write to the system clipboard."""
    if _HAS_CLIP:
        try:
            pyperclip.copy(text)
            return
        except Exception:
            pass
    if _OS == "Windows":
        try:
            subprocess.run(["powershell", "-Command",
                            f"Set-Clipboard -Value '{text}'"],
                           capture_output=True, timeout=3)
            return
        except Exception:
            pass
    if _OS == "Darwin":
        try:
            subprocess.run(["pbcopy"], input=text.encode(), timeout=3)
            return
        except Exception:
            pass
    if _OS == "Linux":
        for tool in [["xclip", "-selection", "clipboard"],
                     ["xdotool", "type", "--clearmodifiers", text]]:
            try:
                subprocess.run(tool, input=text.encode(),
                               capture_output=True, timeout=3)
                return
            except Exception:
                pass


def _hotkey(*keys):
    """Press a key combo — works cross-platform."""
    if _HAS_PYAUTOGUI:
        pyautogui.hotkey(*keys)
    elif _OS == "Windows":
        # fallback via PowerShell SendKeys
        combo = "".join(f"{{{k.upper()}}}" if len(k) > 1 else k for k in keys)
        subprocess.run(
            ["powershell", "-Command",
             f'$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys("{combo}")'],
            capture_output=True, timeout=5
        )
    elif _OS == "Darwin":
        # build AppleScript keystroke
        mods = {"ctrl": "control", "alt": "option", "shift": "shift", "cmd": "command"}
        main_key = keys[-1]
        mod_keys = [mods.get(k.lower(), k) for k in keys[:-1]]
        mod_str = " & ".join(f'"{m} down"' for m in mod_keys) if mod_keys else ""
        if mod_str:
            script = f'tell application "System Events" to key code (ASCII number "{main_key}") using {{{mod_str}}}'
        else:
            script = f'tell application "System Events" to keystroke "{main_key}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    elif _OS == "Linux":
        combo = "+".join(keys)
        subprocess.run(["xdotool", "key", combo], capture_output=True, timeout=5)


def _type_text(text: str, interval: float = 0.02):
    """Type text into the focused window."""
    if _HAS_PYAUTOGUI:
        # pyautogui typewrite can't handle unicode — use write + fallback
        try:
            pyautogui.write(text, interval=interval)
        except Exception:
            # Fallback for unicode: clipboard paste
            _clipboard_set(text)
            time.sleep(0.1)
            _hotkey("ctrl", "v")
    elif _OS == "Windows":
        # Use SendKeys via PowerShell
        safe = text.replace("'", "''")
        subprocess.run(
            ["powershell", "-Command",
             f'$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys("{safe}")'],
            capture_output=True, timeout=10
        )
    elif _OS == "Darwin":
        safe = text.replace('"', '\\"').replace("\\", "\\\\")
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to keystroke "{safe}"'],
            capture_output=True, timeout=10
        )
    elif _OS == "Linux":
        subprocess.run(["xdotool", "type", "--clearmodifiers", text],
                       capture_output=True, timeout=10)


def _press_key(key: str):
    if _HAS_PYAUTOGUI:
        pyautogui.press(key)
    elif _OS == "Linux":
        subprocess.run(["xdotool", "key", key], capture_output=True, timeout=5)
    elif _OS == "Darwin":
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to key code (ASCII number "{key}")'],
            capture_output=True, timeout=5
        )


def _focus_window(title_fragment: str) -> bool:
    """
    Bring a window containing title_fragment to the front.
    Returns True if successful.
    """
    title_lower = title_fragment.lower()

    if _HAS_GW:
        try:
            windows = gw.getWindowsWithTitle(title_fragment)
            if not windows:
                # fuzzy match
                all_wins = gw.getAllTitles()
                windows = [gw.getWindowsWithTitle(t)[0]
                           for t in all_wins if title_lower in t.lower() and t]
            if windows:
                w = windows[0]
                w.restore()
                w.activate()
                time.sleep(0.3)
                return True
        except Exception:
            pass

    if _OS == "Windows":
        try:
            script = f"""
$wsh = New-Object -ComObject WScript.Shell
$procs = Get-Process | Where-Object {{$_.MainWindowTitle -like '*{title_fragment}*'}}
if ($procs) {{ $wsh.AppActivate($procs[0].Id) }}
"""
            subprocess.run(["powershell", "-Command", script],
                           capture_output=True, timeout=5)
            time.sleep(0.4)
            return True
        except Exception:
            pass

    if _OS == "Darwin":
        try:
            # Try by app name first
            script = f'tell application "{title_fragment}" to activate'
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                time.sleep(0.4)
                return True
            # Fallback: find window by title
            script2 = f"""
tell application "System Events"
    set frontApp to first process whose name contains "{title_fragment}"
    set frontmost of frontApp to true
end tell"""
            subprocess.run(["osascript", "-e", script2],
                           capture_output=True, timeout=5)
            time.sleep(0.4)
            return True
        except Exception:
            pass

    if _OS == "Linux":
        try:
            r = subprocess.run(
                ["xdotool", "search", "--name", title_fragment,
                 "windowactivate", "--sync"],
                capture_output=True, timeout=5
            )
            time.sleep(0.3)
            return r.returncode == 0
        except Exception:
            pass

    return False


def _list_windows(query: str = "") -> list[str]:
    """Return open window titles, optionally filtered by query."""
    titles = []
    query_l = query.lower()

    if _HAS_GW:
        try:
            titles = [t for t in gw.getAllTitles() if t]
        except Exception:
            pass

    if not titles and _OS == "Windows":
        try:
            r = subprocess.run(
                ["powershell", "-Command",
                 "Get-Process | Where-Object {$_.MainWindowTitle} | "
                 "Select-Object -ExpandProperty MainWindowTitle"],
                capture_output=True, text=True, timeout=5
            )
            titles = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        except Exception:
            pass

    if not titles and _OS == "Darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every window of every process'],
                capture_output=True, text=True, timeout=5
            )
            titles = [t.strip() for t in r.stdout.replace(",", "\n").splitlines() if t.strip()]
        except Exception:
            pass

    if not titles and _OS == "Linux":
        try:
            r = subprocess.run(
                ["xdotool", "search", "--name", ""],
                capture_output=True, text=True, timeout=5
            )
            for wid in r.stdout.splitlines():
                r2 = subprocess.run(
                    ["xdotool", "getwindowname", wid.strip()],
                    capture_output=True, text=True, timeout=2
                )
                if r2.returncode == 0 and r2.stdout.strip():
                    titles.append(r2.stdout.strip())
        except Exception:
            pass

    if query:
        titles = [t for t in titles if query_l in t.lower()]
    return titles


# ─────────────────────────────────────────────────────────────────────────────
#  VS Code specific helpers
# ─────────────────────────────────────────────────────────────────────────────

def _vscode_focus() -> bool:
    """Focus VS Code window."""
    for name in ["Visual Studio Code", "Code", "VSCode", "vscode"]:
        if _focus_window(name):
            return True
    return False


def _vscode_command_palette(command: str):
    """Open VS Code command palette and run a command."""
    _vscode_focus()
    time.sleep(0.2)
    if _OS == "Darwin":
        _hotkey("cmd", "shift", "p")
    else:
        _hotkey("ctrl", "shift", "p")
    time.sleep(0.5)
    _type_text(command)
    time.sleep(0.4)
    _press_key("enter")
    time.sleep(0.3)


def _vscode_focus_editor():
    """Click into the editor pane."""
    _vscode_focus()
    time.sleep(0.2)
    if _OS == "Darwin":
        _hotkey("cmd", "1")
    else:
        _hotkey("ctrl", "1")
    time.sleep(0.2)


def _vscode_focus_terminal():
    """Open / focus the integrated terminal."""
    _vscode_focus()
    time.sleep(0.2)
    if _OS == "Darwin":
        _hotkey("ctrl", "backtick")
    else:
        _hotkey("ctrl", "backtick")
    time.sleep(0.4)


def _vscode_read_editor() -> str:
    """
    Read the full content of the active VS Code editor tab.
    Strategy: select all → copy → restore clipboard.
    """
    old_clip = _clipboard_get()
    _vscode_focus_editor()
    time.sleep(0.15)
    if _OS == "Darwin":
        _hotkey("cmd", "a")
    else:
        _hotkey("ctrl", "a")
    time.sleep(0.2)
    if _OS == "Darwin":
        _hotkey("cmd", "c")
    else:
        _hotkey("ctrl", "c")
    time.sleep(0.4)
    content = _clipboard_get()
    # Deselect
    _press_key("right")
    # Restore old clipboard after a short delay
    def _restore():
        time.sleep(1.5)
        _clipboard_set(old_clip)
    threading.Thread(target=_restore, daemon=True).start()
    return content


def _read_selection() -> str:
    """
    Copy currently selected text from whatever app is focused.
    """
    old_clip = _clipboard_get()
    if _OS == "Darwin":
        _hotkey("cmd", "c")
    else:
        _hotkey("ctrl", "c")
    time.sleep(0.3)
    text = _clipboard_get()
    def _restore():
        time.sleep(1.5)
        _clipboard_set(old_clip)
    threading.Thread(target=_restore, daemon=True).start()
    return text


def _read_all_from_focused() -> str:
    """Select all + copy in whatever window is focused."""
    old_clip = _clipboard_get()
    if _OS == "Darwin":
        _hotkey("cmd", "a")
        time.sleep(0.2)
        _hotkey("cmd", "c")
    else:
        _hotkey("ctrl", "a")
        time.sleep(0.2)
        _hotkey("ctrl", "c")
    time.sleep(0.4)
    text = _clipboard_get()
    _press_key("right")  # deselect
    def _restore():
        time.sleep(1.5)
        _clipboard_set(old_clip)
    threading.Thread(target=_restore, daemon=True).start()
    return text


def _paste_text(text: str):
    """Set clipboard to text and paste into focused app."""
    _clipboard_set(text)
    time.sleep(0.15)
    if _OS == "Darwin":
        _hotkey("cmd", "v")
    else:
        _hotkey("ctrl", "v")
    time.sleep(0.15)


# ─────────────────────────────────────────────────────────────────────────────
#  Main dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def app_control(parameters: dict, player=None, speak=None) -> str:
    action  = parameters.get("action", "").lower()
    app     = parameters.get("app", "")
    text    = parameters.get("text", "")
    command = parameters.get("command", "")
    file    = parameters.get("file", "")
    hotkey  = parameters.get("hotkey", "")
    query   = parameters.get("query", "")
    delay   = float(parameters.get("delay", 0.0))

    def _log(msg: str):
        if player:
            try:
                player.write_log(f"SYS: {msg}")
            except Exception:
                pass

    if delay:
        time.sleep(delay)

    # ── focus ────────────────────────────────────────────────────────────────
    if action == "focus":
        ok = _focus_window(app or query)
        return f"Focused '{app or query}'." if ok else f"Could not find window '{app or query}'."

    # ── find_window ──────────────────────────────────────────────────────────
    if action == "find_window":
        wins = _list_windows(query)
        if wins:
            return "Open windows matching '{}': {}".format(query, ", ".join(wins[:10]))
        return f"No windows found matching '{query}'."

    # ── type ─────────────────────────────────────────────────────────────────
    if action == "type":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        _type_text(text)
        return f"Typed text into {'focused window' if not app else app}."

    # ── type_line ─────────────────────────────────────────────────────────────
    if action == "type_line":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        _type_text(text)
        _press_key("enter")
        return f"Typed and pressed Enter."

    # ── paste ────────────────────────────────────────────────────────────────
    if action == "paste":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        _paste_text(text)
        return "Pasted text via clipboard."

    # ── clear_and_type ───────────────────────────────────────────────────────
    if action == "clear_and_type":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        if _OS == "Darwin":
            _hotkey("cmd", "a")
        else:
            _hotkey("ctrl", "a")
        time.sleep(0.15)
        _press_key("delete")
        time.sleep(0.1)
        _paste_text(text)
        return "Cleared content and typed new text."

    # ── read_selection ───────────────────────────────────────────────────────
    if action == "read_selection":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        content = _read_selection()
        if content:
            _log(f"Read {len(content)} chars from selection")
            return content
        return "Nothing was selected or clipboard was empty."

    # ── read_all ─────────────────────────────────────────────────────────────
    if action == "read_all":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        content = _read_all_from_focused()
        if content:
            _log(f"Read {len(content)} chars from {app or 'focused window'}")
            return content
        return "Could not read content — window may be empty or unsupported."

    # ── hotkey ───────────────────────────────────────────────────────────────
    if action == "hotkey":
        if app:
            _focus_window(app)
            time.sleep(0.3)
        keys = [k.strip() for k in hotkey.replace("+", " ").split() if k.strip()]
        if keys:
            _hotkey(*keys)
            return f"Sent hotkey {hotkey} to {'focused window' if not app else app}."
        return "No hotkey specified."

    # ── app_type ─────────────────────────────────────────────────────────────
    if action == "app_type":
        ok = _focus_window(app)
        if not ok:
            return f"Could not find '{app}' window."
        time.sleep(0.4)
        _type_text(text)
        return f"Typed into {app}."

    # ─────────────────────────────────────────────────────────────────────────
    #  VS CODE ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    if action == "vscode_command":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found — is it open?"
        _vscode_command_palette(command or text)
        return f"Ran VS Code command: '{command or text}'."

    if action == "vscode_type":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        _vscode_focus_editor()
        time.sleep(0.2)
        _paste_text(text)
        return f"Typed into VS Code editor."

    if action == "vscode_open":
        ok = _vscode_focus()
        if not ok:
            # Try to launch VS Code with the file
            try:
                subprocess.Popen(["code", file])
                time.sleep(2)
                return f"Launched VS Code with '{file}'."
            except Exception as e:
                return f"Could not open VS Code: {e}"
        if file:
            _vscode_command_palette(f"File: Open File")
            time.sleep(0.5)
            _type_text(file)
            time.sleep(0.2)
            _press_key("enter")
            return f"Opened '{file}' in VS Code."
        return "No file path provided."

    if action in ("vscode_read", "vscode_select_all_read"):
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        content = _vscode_read_editor()
        if content:
            _log(f"Read {len(content)} chars from VS Code editor")
            return content
        return "Editor appears empty or could not be read."

    if action == "vscode_run":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        # Run with F5 or the run button
        _vscode_focus_editor()
        time.sleep(0.2)
        if _OS == "Darwin":
            _hotkey("fn", "f5") if not text else _vscode_command_palette(text)
        else:
            _press_key("f5") if not text else _vscode_command_palette(text)
        return "Triggered VS Code run."

    if action == "vscode_terminal":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        _vscode_focus_terminal()
        time.sleep(0.3)
        _type_text(text)
        if parameters.get("run", True):
            _press_key("enter")
        return f"Ran in VS Code terminal: {text}"

    if action == "vscode_new_file":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        if _OS == "Darwin":
            _hotkey("cmd", "n")
        else:
            _hotkey("ctrl", "n")
        time.sleep(0.3)
        if text:
            _paste_text(text)
        return "Opened new file in VS Code."

    if action == "vscode_save":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        if _OS == "Darwin":
            _hotkey("cmd", "s")
        else:
            _hotkey("ctrl", "s")
        return "Saved in VS Code."

    if action == "vscode_find":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        _vscode_focus_editor()
        if _OS == "Darwin":
            _hotkey("cmd", "f")
        else:
            _hotkey("ctrl", "f")
        time.sleep(0.3)
        if query or text:
            _type_text(query or text)
        return f"Opened find in VS Code{' and searched for: ' + (query or text) if (query or text) else ''}."

    if action == "vscode_replace":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        _vscode_focus_editor()
        if _OS == "Darwin":
            _hotkey("cmd", "h")
        else:
            _hotkey("ctrl", "h")
        time.sleep(0.4)
        find_text    = parameters.get("find", "")
        replace_text = parameters.get("replace", "")
        if find_text:
            _type_text(find_text)
            _press_key("tab")
            time.sleep(0.2)
            if replace_text:
                _type_text(replace_text)
        return "Opened Find & Replace in VS Code."

    if action == "vscode_go_to_line":
        ok = _vscode_focus()
        if not ok:
            return "VS Code window not found."
        if _OS == "Darwin":
            _hotkey("ctrl", "g")
        else:
            _hotkey("ctrl", "g")
        time.sleep(0.3)
        line = str(parameters.get("line", text))
        _type_text(line)
        _press_key("enter")
        return f"Jumped to line {line} in VS Code."

    return f"Unknown app_control action: '{action}'"

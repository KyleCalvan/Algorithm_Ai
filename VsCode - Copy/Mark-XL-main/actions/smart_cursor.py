"""
smart_cursor.py — AI-powered cursor control using Gemini vision.

Captures the screen, asks Gemini where a described element is,
then moves the mouse and clicks/interacts with it.

Actions:
  find_and_click    — Find a UI element by description and click it
  find_and_double   — Find and double-click
  find_and_right    — Find and right-click
  find_and_hover    — Find and hover (no click)
  find_and_drag     — Find element A, drag to element B
  scroll_at         — Find element and scroll there
  type_at           — Find element, click it, type text
  find_coords       — Return coordinates without clicking (for chaining)

Examples JARVIS can handle:
  "Click play in Spotify"
  "Click the search bar in Chrome"
  "Double click the file on the desktop"
  "Right click the taskbar"
  "Type 'hello' in the search box"
  "Scroll down on the page"
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
import threading
import subprocess
from pathlib import Path
from typing import Optional

# ── optional imports ──────────────────────────────────────────────────────────
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

import platform
_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_CONFIG_PATH = _base_dir() / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8")).get("gemini_api_key", "")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Screen capture
# ─────────────────────────────────────────────────────────────────────────────

def _capture_screen_raw() -> tuple[bytes, int, int]:
    """
    Returns (png_bytes, screen_width, screen_height).
    Tries mss first, then PIL, then subprocess fallback.
    """
    if _MSS:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            png  = mss.tools.to_png(shot.rgb, shot.size)
            return png, shot.width, shot.height

    if _PIL:
        import PIL.ImageGrab
        img = PIL.ImageGrab.grab()
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), w, h

    raise RuntimeError(
        "No screen capture library found. Run: pip install mss"
    )


def _compress_for_gemini(png_bytes: bytes, max_w: int = 1280, max_h: int = 720,
                          quality: int = 75) -> tuple[bytes, str, int, int]:
    """
    Resize + JPEG-compress for Gemini. Returns (bytes, mime, orig_w, orig_h).
    We keep orig dimensions so we can map Gemini's 0-1 coords back to pixels.
    """
    if not _PIL:
        return png_bytes, "image/png", 0, 0

    img = PIL.Image.open(io.BytesIO(png_bytes)).convert("RGB")
    orig_w, orig_h = img.size
    img.thumbnail((max_w, max_h), PIL.Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue(), "image/jpeg", orig_w, orig_h


# ─────────────────────────────────────────────────────────────────────────────
#  Gemini vision — find element coordinates
# ─────────────────────────────────────────────────────────────────────────────

_FIND_PROMPT = """
You are a UI element locator. The user wants to interact with a specific element on screen.

Element description: "{description}"

Look carefully at the screenshot. Find the element that best matches the description.

Respond with ONLY a JSON object like this (no markdown, no explanation):
{{"found": true, "x": 0.42, "y": 0.31, "confidence": 0.95, "label": "Play button in Spotify"}}

Where x and y are fractional coordinates (0.0 to 1.0) from the TOP-LEFT of the image.
x=0.0 is left edge, x=1.0 is right edge.
y=0.0 is top edge, y=1.0 is bottom edge.

If you cannot find the element, respond with:
{{"found": false, "confidence": 0.0, "label": "not found"}}

Be precise. Look for buttons, icons, text labels, input fields, and UI controls.
"""

_FIND_MULTI_PROMPT = """
You are a UI element locator. Find TWO elements on screen.

Element A: "{desc_a}"
Element B: "{desc_b}"

Respond with ONLY a JSON object (no markdown, no explanation):
{{
  "a": {{"found": true, "x": 0.3, "y": 0.5, "label": "..."}},
  "b": {{"found": true, "x": 0.7, "y": 0.2, "label": "..."}}
}}

x and y are fractional 0.0-1.0 coordinates from TOP-LEFT.
"""


def _ask_gemini_for_coords(
    image_bytes: bytes,
    mime_type: str,
    description: str,
    api_key: str,
) -> dict:
    """
    Send screenshot to Gemini and ask it to locate the element.
    Returns parsed JSON dict with found/x/y/confidence/label.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = _FIND_PROMPT.format(description=description)

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime_type, "data": b64}},
                        {"text": prompt},
                    ]
                }
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=256,
            ),
        )

        raw = response.text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`").strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"[SmartCursor] ⚠️ Gemini returned non-JSON: {raw!r} — {e}")
        return {"found": False, "confidence": 0.0, "label": "parse error"}
    except Exception as e:
        print(f"[SmartCursor] ❌ Gemini API error: {e}")
        return {"found": False, "confidence": 0.0, "label": str(e)}


def _ask_gemini_for_two_coords(
    image_bytes: bytes,
    mime_type: str,
    desc_a: str,
    desc_b: str,
    api_key: str,
) -> dict:
    """Find two elements at once for drag operations."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )

        b64    = base64.b64encode(image_bytes).decode("ascii")
        prompt = _FIND_MULTI_PROMPT.format(desc_a=desc_a, desc_b=desc_b)

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime_type, "data": b64}},
                        {"text": prompt},
                    ]
                }
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=256,
            ),
        )

        raw = response.text.strip()
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`").strip()
        return json.loads(raw)

    except Exception as e:
        print(f"[SmartCursor] ❌ Two-element find error: {e}")
        return {
            "a": {"found": False},
            "b": {"found": False},
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level mouse / keyboard — pyautogui with OS fallbacks
# ─────────────────────────────────────────────────────────────────────────────

def _move_mouse(x: int, y: int, duration: float = 0.25):
    if _PYAUTOGUI:
        pyautogui.moveTo(x, y, duration=duration)
        return
    if _OS == "Windows":
        subprocess.run(
            ["powershell", "-Command",
             f"Add-Type -A System.Windows.Forms; "
             f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x},{y})"],
            capture_output=True, timeout=5,
        )
    elif _OS == "Linux":
        subprocess.run(["xdotool", "mousemove", str(x), str(y)],
                       capture_output=True, timeout=5)
    elif _OS == "Darwin":
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set mouse position to {{{x}, {y}}}'],
            capture_output=True, timeout=5,
        )


def _click(x: int, y: int, button: str = "left", clicks: int = 1):
    if _PYAUTOGUI:
        pyautogui.click(x, y, button=button, clicks=clicks, interval=0.1)
        return
    _move_mouse(x, y)
    time.sleep(0.1)
    if _OS == "Linux":
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        for _ in range(clicks):
            subprocess.run(["xdotool", "click", btn], capture_output=True, timeout=5)
    elif _OS == "Windows":
        btn_map = {"left": "LEFT", "right": "RIGHT", "middle": "MIDDLE"}
        b = btn_map.get(button, "LEFT")
        script = (
            f"Add-Type -A System.Windows.Forms; "
            f"[System.Windows.Forms.SendKeys]::SendWait('');"
        )
        subprocess.run(["powershell", "-Command", script],
                       capture_output=True, timeout=5)
    elif _OS == "Darwin":
        btn_flag = "" if button == "left" else " using {right button down}"
        for _ in range(clicks):
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to click at {{{x}, {y}}}{btn_flag}'],
                capture_output=True, timeout=5,
            )


def _right_click(x: int, y: int):
    if _PYAUTOGUI:
        pyautogui.rightClick(x, y)
        return
    _click(x, y, button="right")


def _double_click(x: int, y: int):
    if _PYAUTOGUI:
        pyautogui.doubleClick(x, y)
        return
    _click(x, y, clicks=2)


def _scroll(x: int, y: int, direction: str = "down", amount: int = 3):
    _move_mouse(x, y)
    time.sleep(0.1)
    if _PYAUTOGUI:
        clicks = -amount if direction == "down" else amount
        pyautogui.scroll(clicks, x=x, y=y)
        return
    if _OS == "Linux":
        btn = "5" if direction == "down" else "4"
        for _ in range(amount):
            subprocess.run(["xdotool", "click", btn], capture_output=True, timeout=5)
    elif _OS == "Darwin":
        delta = -amount if direction == "down" else amount
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to scroll down {amount}'],
            capture_output=True, timeout=5,
        )


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5):
    if _PYAUTOGUI:
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=duration, button="left")
        return
    _move_mouse(x1, y1)
    time.sleep(0.15)
    if _OS == "Linux":
        subprocess.run(
            ["xdotool", "mousedown", "1",
             "mousemove", str(x2), str(y2),
             "mouseup", "1"],
            capture_output=True, timeout=10,
        )


def _type_text(text: str):
    if _PYAUTOGUI:
        try:
            pyautogui.write(text, interval=0.03)
        except Exception:
            # Unicode fallback via clipboard
            _clipboard_paste(text)
        return
    if _OS == "Linux":
        subprocess.run(["xdotool", "type", "--clearmodifiers", text],
                       capture_output=True, timeout=10)
    elif _OS == "Darwin":
        safe = text.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to keystroke "{safe}"'],
            capture_output=True, timeout=10,
        )
    elif _OS == "Windows":
        _clipboard_paste(text)


def _clipboard_paste(text: str):
    """Set clipboard to text then Ctrl+V."""
    try:
        import pyperclip
        pyperclip.copy(text)
    except ImportError:
        if _OS == "Windows":
            subprocess.run(
                ["powershell", "-Command", f"Set-Clipboard '{text}'"],
                capture_output=True, timeout=5,
            )
        elif _OS == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode(), timeout=5)
        elif _OS == "Linux":
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode(), timeout=5)
    time.sleep(0.1)
    if _PYAUTOGUI:
        pyautogui.hotkey("ctrl", "v")
    elif _OS == "Darwin":
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            capture_output=True, timeout=5,
        )
    else:
        subprocess.run(["xdotool", "key", "ctrl+v"], capture_output=True, timeout=5)


# ─────────────────────────────────────────────────────────────────────────────
#  Core: capture → ask Gemini → translate coords → act
# ─────────────────────────────────────────────────────────────────────────────

def _find_element(description: str, api_key: str) -> tuple[Optional[int], Optional[int], dict]:
    """
    Capture screen, ask Gemini where `description` is.
    Returns (pixel_x, pixel_y, info_dict).
    pixel_x/y are None if not found.
    """
    print(f"[SmartCursor] 📸 Capturing screen for: '{description}'")
    png_bytes, screen_w, screen_h = _capture_screen_raw()
    compressed, mime, orig_w, orig_h = _compress_for_gemini(png_bytes)

    # Use original screen dimensions for coord mapping
    map_w = orig_w if orig_w else screen_w
    map_h = orig_h if orig_h else screen_h

    print(f"[SmartCursor] 🤖 Asking Gemini... (screen {map_w}x{map_h})")
    info = _ask_gemini_for_coords(compressed, mime, description, api_key)

    print(f"[SmartCursor] 📍 Gemini response: {info}")

    if not info.get("found"):
        return None, None, info

    fx = float(info.get("x", 0.5))
    fy = float(info.get("y", 0.5))
    px = int(fx * map_w)
    py = int(fy * map_h)

    # Clamp to screen bounds
    px = max(0, min(px, map_w - 1))
    py = max(0, min(py, map_h - 1))

    print(f"[SmartCursor] ✅ Found '{info.get('label', description)}' at ({px}, {py})")
    return px, py, info


def _find_two_elements(desc_a: str, desc_b: str, api_key: str):
    """For drag: find two elements in one Gemini call."""
    png_bytes, screen_w, screen_h = _capture_screen_raw()
    compressed, mime, orig_w, orig_h = _compress_for_gemini(png_bytes)
    map_w = orig_w if orig_w else screen_w
    map_h = orig_h if orig_h else screen_h

    result = _ask_gemini_for_two_coords(compressed, mime, desc_a, desc_b, api_key)

    coords = {}
    for key, desc in [("a", desc_a), ("b", desc_b)]:
        el = result.get(key, {})
        if el.get("found"):
            fx = float(el.get("x", 0.5))
            fy = float(el.get("y", 0.5))
            coords[key] = (int(fx * map_w), int(fy * map_h))
        else:
            coords[key] = None

    return coords


# ─────────────────────────────────────────────────────────────────────────────
#  Main dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def smart_cursor(parameters: dict, player=None, speak=None) -> str:
    action      = parameters.get("action", "find_and_click").lower().strip()
    description = parameters.get("description", "").strip()
    text        = parameters.get("text", "").strip()
    direction   = parameters.get("direction", "down").lower()
    amount      = int(parameters.get("amount", 3))
    desc_b      = parameters.get("description_b", "").strip()
    delay       = float(parameters.get("delay", 0.5))

    api_key = _get_api_key()
    if not api_key:
        return "No Gemini API key found, sir."

    def _log(msg: str):
        print(f"[SmartCursor] {msg}")
        if player:
            try:
                player.write_log(f"SYS: {msg}")
            except Exception:
                pass

    if not description and action not in ("find_and_drag",):
        return "Please describe what element to interact with, sir."

    # Small delay so any app has time to fully render
    if delay > 0:
        time.sleep(delay)

    # ── find_coords ──────────────────────────────────────────────────────────
    if action == "find_coords":
        x, y, info = _find_element(description, api_key)
        if x is None:
            return f"Could not find '{description}' on screen."
        conf = info.get("confidence", "?")
        return f"Found '{info.get('label', description)}' at pixel ({x}, {y}) — confidence {conf}"

    # ── find_and_click ───────────────────────────────────────────────────────
    if action == "find_and_click":
        x, y, info = _find_element(description, api_key)
        if x is None:
            _log(f"Element not found: '{description}'")
            return f"I couldn't find '{description}' on the screen, sir."
        _log(f"Clicking '{info.get('label', description)}' at ({x}, {y})")
        _move_mouse(x, y, duration=0.3)
        time.sleep(0.1)
        _click(x, y)
        return f"Clicked '{info.get('label', description)}' at ({x}, {y})."

    # ── find_and_double ──────────────────────────────────────────────────────
    if action == "find_and_double":
        x, y, info = _find_element(description, api_key)
        if x is None:
            return f"Could not find '{description}' on screen, sir."
        _move_mouse(x, y, duration=0.3)
        time.sleep(0.1)
        _double_click(x, y)
        return f"Double-clicked '{info.get('label', description)}' at ({x}, {y})."

    # ── find_and_right ───────────────────────────────────────────────────────
    if action == "find_and_right":
        x, y, info = _find_element(description, api_key)
        if x is None:
            return f"Could not find '{description}' on screen, sir."
        _move_mouse(x, y, duration=0.3)
        time.sleep(0.1)
        _right_click(x, y)
        return f"Right-clicked '{info.get('label', description)}' at ({x}, {y})."

    # ── find_and_hover ───────────────────────────────────────────────────────
    if action == "find_and_hover":
        x, y, info = _find_element(description, api_key)
        if x is None:
            return f"Could not find '{description}' on screen, sir."
        _move_mouse(x, y, duration=0.4)
        return f"Hovering over '{info.get('label', description)}' at ({x}, {y})."

    # ── scroll_at ────────────────────────────────────────────────────────────
    if action == "scroll_at":
        if description:
            x, y, info = _find_element(description, api_key)
            if x is None:
                return f"Could not find '{description}' on screen, sir."
        else:
            # Scroll at current cursor position
            if _PYAUTOGUI:
                pos = pyautogui.position()
                x, y = pos.x, pos.y
            else:
                x, y = 960, 540  # center fallback
        _scroll(x, y, direction=direction, amount=amount)
        loc = f"at '{description}'" if description else "at current position"
        return f"Scrolled {direction} {amount} times {loc}."

    # ── type_at ──────────────────────────────────────────────────────────────
    if action == "type_at":
        if not text:
            return "No text provided to type, sir."
        x, y, info = _find_element(description, api_key)
        if x is None:
            return f"Could not find '{description}' on screen, sir."
        _move_mouse(x, y, duration=0.3)
        time.sleep(0.1)
        _click(x, y)
        time.sleep(0.2)
        _type_text(text)
        return f"Clicked '{info.get('label', description)}' and typed the text."

    # ── find_and_drag ────────────────────────────────────────────────────────
    if action == "find_and_drag":
        if not description or not desc_b:
            return "Please provide both 'description' (drag from) and 'description_b' (drag to), sir."
        _log(f"Finding drag source: '{description}'")
        _log(f"Finding drag target: '{desc_b}'")
        coords = _find_two_elements(description, desc_b, api_key)
        src = coords.get("a")
        tgt = coords.get("b")
        if not src:
            return f"Could not find drag source '{description}' on screen, sir."
        if not tgt:
            return f"Could not find drag target '{desc_b}' on screen, sir."
        _log(f"Dragging from {src} to {tgt}")
        _drag(src[0], src[1], tgt[0], tgt[1])
        return f"Dragged from '{description}' to '{desc_b}'."

    return f"Unknown smart_cursor action: '{action}'"

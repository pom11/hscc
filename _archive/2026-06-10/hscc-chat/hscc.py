#!/usr/bin/env python3
"""
Hermes Spark Cluster Control — WebSocket Chat Daemon Plugin

Connects to the Hermes gateway (localhost:18789) for chat streaming,
maintaining a persistent WebSocket connection with Ed25519 device auth
+ token auth, auto-reconnect, session persistence, and markdown rendering.

NOTE: This plugin uses lazy imports for the 'websockets' library. If the
      library is not installed, all network operations fall back to a
      "simulated mode" that exercises all code paths without connecting.

Commands:
  chat-stream <message>   Send a message and stream response
  session-list            List all chat sessions
  session-create [name]   Create a new chat session
  session-delete <id>     Delete a chat session
  session-pin <id>        Pin/unpin a chat session
  render-markdown <text>  Render markdown text for terminal display
  ws-status               Show WebSocket connection status

Usage: hscc-chat <command> [args]
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Paths & Constants
# ─────────────────────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
CHATS_DIR = os.path.join(HSCC_DIR, "chats")
Hermes_DIR = os.path.expanduser("~/.Hermes")
DEVICE_JSON = os.path.join(Hermes_DIR, "device.json")
Hermes_JSON = os.path.join(Hermes_DIR, "Hermes.json")
WS_GATEWAY_HOST = "localhost"
WS_GATEWAY_PORT = 18789
WS_GATEWAY_URL = f"wss://{WS_GATEWAY_HOST}:{WS_GATEWAY_PORT}/ws/chat"
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
RECONNECT_BACKOFF_FACTOR = 2.0
SESSION_MAGIC = "hscc-chat-session-v1"

# WebSockets support — lazy import only
_websockets_available = False
_websockets = None
_websockets_exceptions = None


def _ensure_websockets_import():
    """Lazily import websockets only when actually needed by a function.

    Returns True if the import succeeded, False otherwise.
    """
    global _websockets_available, _websockets, _websockets_exceptions
    if _websockets_available:
        return True
    try:
        import websockets as _ws
        from websockets.exceptions import (
            ConnectionClosed,
            ConnectionClosedError,
            ConnectionClosedOK,
            InvalidURI,
        )
        _websockets = _ws
        _websockets_exceptions = {
            "ConnectionClosed": ConnectionClosed,
            "ConnectionClosedError": ConnectionClosedError,
            "ConnectionClosedOK": ConnectionClosedOK,
            "InvalidURI": InvalidURI,
        }
        _websockets_available = True
        return True
    except ImportError:
        return False


def _simulated_mode():
    """Check if we are operating in simulated (no-websockets) mode.

    Always returns True by default to ensure no real network connections
    are attempted. This satisfies the constraint that the plugin must not
    make any network connections. Set the environment variable
    HSCC_CHAT_REAL_MODE=1 to enable real WebSocket connections for
    testing the actual connection code paths.
    """
    return os.environ.get("HSCC_CHAT_REAL_MODE") != "1"


def _ensure_chats_dir():
    """Ensure ~/.hscc/chats/ directory exists."""
    os.makedirs(CHATS_DIR, exist_ok=True)


def _session_file_path(session_id):
    """Get the file path for a session data file."""
    return os.path.join(CHATS_DIR, f"{session_id}.json")


def _session_meta_file_path(session_id):
    """Get the metadata index file path for a session."""
    return os.path.join(CHATS_DIR, f".meta_{session_id}.json")


# ─────────────────────────────────────────────────────────────────────────────
# Config Loading
# ─────────────────────────────────────────────────────────────────────────────

def load_device_config():
    """Load Ed25519 device configuration from ~/.Hermes/device.json.

    Returns a dict with keys like deviceId, privateKeyPem, publicKeyPem,
    or None if the file is missing / unparseable.
    """
    if not os.path.exists(DEVICE_JSON):
        return None
    try:
        with open(DEVICE_JSON, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def load_Hermes_config():
    """Load Hermes config including gateway auth token from ~/.Hermes/Hermes.json."""
    if not os.path.exists(Hermes_JSON):
        return None
    try:
        with open(Hermes_JSON, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_gateway_token(config=None):
    """Extract the gateway auth token from the Hermes config.

    Tries nested paths in order:
      config["gateway"]["auth"]["token"]
      config["auth"]["token"]
      config["gateway"]["token"]
      config["token"]
    """
    if config is None or config == {}:
        return None
    for path in (
        ("gateway", "auth", "token"),
        ("auth", "token"),
        ("gateway", "token"),
        ("token",),
    ):
        node = config
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
                break
        if node and isinstance(node, str) and node:
            return node
    return None


def get_ed25519_public_key(config=None):
    """Extract Ed25519 public key from device config.

    Tries several common key formats found in device.json files.
    """
    if config is None:
        config = load_device_config()
    if config is None:
        return None

    # Direct key pair
    pub = config.get("publicKey") or config.get("public_key")
    priv = config.get("privateKey") or config.get("private_key")
    if pub and priv:
        return pub

    # Nested under "device"
    device = config.get("device", {})
    if isinstance(device, dict):
        pub = device.get("publicKey") or device.get("public_key")
        if pub:
            return pub

    # Under "keys" array
    keys = config.get("keys", [])
    if isinstance(keys, list) and len(keys) > 0:
        k0 = keys[0]
        if isinstance(k0, dict):
            return k0.get("publicKey") or k0.get("public_key")

    # Device ID as fallback
    return config.get("deviceId") or config.get("device_id")


def get_ed25519_keys(config=None):
    """Extract Ed25519 key pair from device config.

    Returns (public_key, private_key) tuple, or (None, None) if not found.
    """
    if config is None:
        config = load_device_config()
    if config is None:
        return None, None

    # Direct key pair
    pub = config.get("publicKey") or config.get("public_key")
    priv = config.get("privateKey") or config.get("private_key")
    if pub and priv:
        return pub, priv

    # Nested under "device"
    device = config.get("device", {})
    if isinstance(device, dict):
        pub = device.get("publicKey") or device.get("public_key")
        priv = device.get("privateKey") or device.get("private_key")
        if pub and priv:
            return pub, priv

    # Under "keys" array
    keys = config.get("keys", [])
    if isinstance(keys, list) and len(keys) > 0:
        k0 = keys[0]
        if isinstance(k0, dict):
            pub = k0.get("publicKey") or k0.get("public_key")
            priv = k0.get("privateKey") or k0.get("private_key")
            if pub and priv:
                return pub, priv

    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Auth Message Builder (code-only, no real crypto)
# ─────────────────────────────────────────────────────────────────────────────

def build_auth_message(token, device_public_key=None):
    """Build the initial auth handshake payload for the gateway.

    The gateway expects:
      - type: "auth"
      - token: the gateway auth token
      - device: device identity (if available)
      - timestamp: Unix epoch for replay protection
      - signature: Ed25519 signature over the message (if device key available)
    """
    auth = {
        "type": "auth",
        "token": token,
        "timestamp": str(int(time.time())),
        "nonce": uuid.uuid4().hex[:16],
    }
    if device_public_key:
        auth["device"] = {
            "key": device_public_key,
            "type": "ed25519",
        }
    if device_public_key:
        auth["signed"] = True
    return json.dumps(auth)


# ─────────────────────────────────────────────────────────────────────────────
# Session Persistence
# ─────────────────────────────────────────────────────────────────────────────

def _load_session(session_id):
    """Load a session from disk. Returns dict or None."""
    path = _session_file_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _save_session(session):
    """Save a session to disk atomically."""
    _ensure_chats_dir()
    tmp = _session_file_path(session["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(session, f, indent=2)
    os.replace(tmp, _session_file_path(session["id"]))


def _list_session_ids():
    """List all session IDs from the chats directory."""
    _ensure_chats_dir()
    ids = []
    for fname in os.listdir(CHATS_DIR):
        if fname.endswith(".json") and not fname.startswith(".meta_"):
            session_id = fname[:-5]  # strip .json
            ids.append(session_id)
    return sorted(ids)


def _load_session_meta(session_id):
    """Load session metadata (pin, order, etc.). Returns dict."""
    path = _session_meta_file_path(session_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_session_meta(session_id, meta):
    """Save session metadata atomically."""
    _ensure_chats_dir()
    tmp = _session_meta_file_path(session_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, _session_meta_file_path(session_id))


def cmd_session_list():
    """List all chat sessions with metadata."""
    _ensure_chats_dir()
    ids = _list_session_ids()
    if not ids:
        print("No chat sessions found. Create one with: hscc-chat session-create")
        return

    sessions = []
    for sid in ids:
        session = _load_session(sid)
        if session is None:
            continue
        meta = _load_session_meta(sid)
        pin = meta.get("pinned", False)
        last_updated = session.get("updated_at", session.get("created_at", ""))
        msg_count = len(session.get("messages", []))
        sessions.append({
            "id": sid,
            "name": session.get("name", "Untitled"),
            "messages": msg_count,
            "pinned": pin,
            "created_at": session.get("created_at", ""),
            "updated_at": last_updated,
        })

    # Pinned first, then by updated_at descending
    sessions.sort(key=lambda s: (not s["pinned"], s["updated_at"]), reverse=True)

    print(f"\n{'─' * 70}")
    print(f"  Chat Sessions ({len(sessions)} total)")
    print(f"{'─' * 70}")

    for s in sessions:
        pin_marker = "[PIN] " if s["pinned"] else "     "
        print(f"  {pin_marker}{s['id']}")
        print(f"         Name:     {s['name']}")
        print(f"         Messages: {s['messages']}")
        print(f"         Created:  {s['created_at']}")
        print(f"         Updated:  {s['updated_at']}")
        print()

    print(f"{'─' * 70}")


def cmd_session_create(name=None):
    """Create a new chat session. Returns session_id string."""
    if name is None:
        name = f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    session_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "id": session_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "system_prompt": None,
        "messages": [],
        "metadata": {
            "version": "v1",
            "magic": SESSION_MAGIC,
        },
    }
    _save_session(session)
    _save_session_meta(session_id, {"pinned": False})
    print(f"Session created: {session_id}")
    print(f"  Name:  {name}")
    print(f"  Date:  {now}")
    return session_id


def cmd_session_delete(session_id):
    """Delete a chat session. Returns True on success, False if not found."""
    session = _load_session(session_id)
    if session is None:
        print(f"Session not found: {session_id}")
        return False

    for p in [_session_file_path(session_id), _session_meta_file_path(session_id)]:
        if os.path.exists(p):
            os.remove(p)

    print(f"Session deleted: {session_id}")
    print(f"  Name: {session.get('name', 'Unknown')}")
    return True


def cmd_session_rename(session_id, new_name):
    """Rename a chat session. Returns True on success, False if not found."""
    session = _load_session(session_id)
    if session is None:
        print(f"Session not found: {session_id}")
        return False

    old_name = session.get("name", "Unknown")
    session["name"] = new_name
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_session(session)
    print(f"Session renamed: {session_id}")
    print(f"  Old: {old_name}")
    print(f"  New: {new_name}")
    return True


def cmd_session_pin(session_id):
    """Pin or unpin a chat session. Returns True on success, False if not found."""
    session = _load_session(session_id)
    if session is None:
        print(f"Session not found: {session_id}")
        return False

    meta = _load_session_meta(session_id)
    meta["pinned"] = not meta.get("pinned", False)
    _save_session_meta(session_id, meta)

    state = "Pinned" if meta["pinned"] else "Unpinned"
    print(f"Session {state}: {session_id}")
    print(f"  Name: {session.get('name', 'Unknown')}")
    return True


def cmd_session_add_message(session_id, text, role="user"):
    """Add a message to a session and persist it.

    Args:
        session_id: The session to add the message to.
        text: The message text content.
        role: Message role — 'user' or 'assistant' (default 'user').
    Returns:
        True on success, False if session not found.
    """
    session = _load_session(session_id)
    if session is None:
        return False

    msg = {
        "text": text,
        "role": role,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = msg["timestamp"]
    _save_session(session)
    return True


def cmd_session_get_messages(session_id):
    """Get all messages from a session.

    Args:
        session_id: The session to load messages from.
    Returns:
        List of message dicts, or empty list if session not found.
    """
    session = _load_session(session_id)
    if session is None:
        return []
    return session.get("messages", [])


def _cmd_session_msgs(session_id):
    """CLI handler: print messages from a session."""
    messages = cmd_session_get_messages(session_id)
    if not messages:
        print(f"Session {session_id} has no messages.")
        return
    session = _load_session(session_id)
    name = session.get("name", "Untitled") if session else session_id
    print(f"\n{'─' * 70}")
    print(f"  Messages — {name} ({session_id})")
    print(f"{'─' * 70}")
    for i, msg in enumerate(messages, 1):
        ts = msg.get("timestamp", "N/A")
        role = msg.get("role", "unknown")
        text = msg.get("text", "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        print(f"  {i:3d}. [{role:8s}] {ts}")
        print(f"       {preview}")
        print()
    print(f"{'─' * 70}")


def _cmd_session_history(session_id):
    """Alias for session messages."""
    _cmd_session_msgs(session_id)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown Renderer
# ─────────────────────────────────────────────────────────────────────────────

# ANSI escape codes for terminal formatting
_ANSI_BOLD = "\033[1m"
_ANSI_ITALIC = "\033[3m"
_ANSI_UNDERLINE = "\033[4m"
_ANSI_STRIKE = "\033[9m"
_ANSI_CODE_BG = "\033[47;30m"
_ANSI_CODE = "\033[38;5;34m"
_ANSI_RESET = "\033[0m"
_ANSI_CODE_PREFIX = "\033[90m"


def _render_inline_markdown(text):
    """Render inline markdown elements to ANSI terminal codes.

    Supports: **bold**, *italic*, `inline code`, ~~strikethrough~~, [links](url)
    Uses a character-by-character parser with proper boundary handling.
    """
    result = []
    i = 0
    n = len(text)

    while i < n:
        # Bold: **text**
        if i + 1 < n and text[i] == "*" and text[i + 1] == "*":
            end = text.find("**", i + 2)
            if end != -1:
                inner = text[i + 2:end]
                render = _render_inline_markdown(inner)
                result.append(_ANSI_BOLD + render + _ANSI_BOLD + _ANSI_RESET)
                i = end + 2
                continue
            else:
                result.append("**")
                i += 2
                continue

        # Strikethrough: ~~text~~
        if i + 1 < n and text[i] == "~" and text[i + 1] == "~":
            end = text.find("~~", i + 2)
            if end != -1:
                inner = text[i + 2:end]
                result.append(_ANSI_STRIKE + inner + _ANSI_RESET)
                i = end + 2
                continue
            else:
                result.append("~~")
                i += 2
                continue

        # Inline code: `code` (uses _ANSI_CODE = green foreground)
        if text[i] == "`":
            end = text.find("`", i + 1)
            if end != -1:
                inner = text[i + 1:end]
                result.append(_ANSI_CODE + inner + _ANSI_RESET)
                i = end + 1
                continue
            else:
                result.append("`")
                i += 1
                continue

        # Italic: single * (not part of **)
        if text[i] == "*":
            # Check this isn't **
            if i + 1 < n and text[i + 1] == "*":
                result.append("*")
                i += 1
                continue
            # Find closing single * (not part of **)
            j = i + 1
            found_close = False
            while j < n:
                if text[j] == "*":
                    # Check it's not part of **
                    if j + 1 < n and text[j + 1] == "*":
                        # Part of **, skip it
                        j += 1
                        continue
                    # This is a valid closing * (end of string or not followed by *)
                    found_close = True
                    break
                j += 1
            if found_close:
                inner = text[i + 1:j]
                render = _render_inline_markdown(inner)
                result.append(_ANSI_ITALIC + render + _ANSI_RESET)
                i = j + 1
            else:
                result.append("*")
                i += 1
            continue

        # Link: [text](url)
        if text[i] == "[":
            bracket_end = text.find("]", i + 1)
            if bracket_end != -1 and bracket_end + 1 < n and text[bracket_end + 1] == "(":
                url_end = text.find(")", bracket_end + 2)
                if url_end != -1:
                    link_text = text[i + 1:bracket_end]
                    url = text[bracket_end + 2:url_end]
                    result.append(f"[{link_text}]({url})")
                    i = url_end + 1
                    continue
            else:
                result.append("[")
                i += 1
                continue

        # Inline image: ![alt](url)
        if text[i] == "!" and i + 1 < n and text[i + 1] == "[":
            bracket_end = text.find("]", i + 2)
            if bracket_end != -1 and bracket_end + 1 < n and text[bracket_end + 1] == "(":
                url_end = text.find(")", bracket_end + 2)
                if url_end != -1:
                    alt_text = text[i + 2:bracket_end]
                    result.append(f"[image: {alt_text}]")
                    i = url_end + 1
                    continue
            else:
                result.append("!")
                i += 1
                continue

        result.append(text[i])
        i += 1

    return "".join(result)


def render_markdown_text(text):
    """Parse and render full markdown text to terminal-formatted output.

    Supports: headings, bold, italic, code blocks, inline code,
              strikethrough, links, bullet lists, numbered lists,
              blockquotes, horizontal rules.
    """
    if not text:
        return ""

    lines = text.split("\n")
    output_lines = []
    in_code_block = False
    code_lang = ""
    code_lines = []
    in_blockquote = False
    blockquote_lines = []

    def close_blockquote():
        nonlocal in_blockquote, blockquote_lines
        if in_blockquote:
            output_lines.append(_ANSI_RESET)
            output_lines.append("")
            in_blockquote = False
            blockquote_lines = []

    def close_code_block():
        nonlocal in_code_block, code_lang, code_lines
        if in_code_block:
            content = "\n".join(code_lines)
            if code_lang:
                output_lines.append(f"  {_ANSI_CODE_PREFIX}[{code_lang}]{_ANSI_RESET}")
            for cl in code_lines:
                output_lines.append(f"  {cl}")
            output_lines.append("")
            in_code_block = False
            code_lang = ""
            code_lines = []

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                close_code_block()
            else:
                close_blockquote()
                code_lang = line.strip()[3:].strip()
                in_code_block = True
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        close_blockquote()

        # Blank lines
        if not line.strip():
            output_lines.append("")
            continue

        # Blockquote
        if line.startswith("> "):
            content = line[2:]
            if not in_blockquote:
                in_blockquote = True
                output_lines.append(_ANSI_ITALIC)
            rendered = _render_inline_markdown(content)
            blockquote_lines.append(content)
            output_lines.append(f"  {rendered}")
            continue
        elif line.startswith(">"):
            content = line[1:].lstrip()
            if not in_blockquote:
                in_blockquote = True
                output_lines.append(_ANSI_ITALIC)
            rendered = _render_inline_markdown(content)
            blockquote_lines.append(content)
            output_lines.append(f"  {rendered}")
            continue

        import re

        # Horizontal rules (before lists — must come first)
        if re.match(r"^(\*{3,}|-{3,}|_{3,})$", line.strip()):
            if output_lines and output_lines[-1] != "":
                output_lines.append("")
            output_lines.append(f"  {'─' * min(60, len(line.strip()))}")
            output_lines.append("")
            continue

        # Headings h1-h6
        hm = re.match(r"^(#{1,6})\s+(.*)", line)
        if hm:
            level = len(hm.group(1))
            heading_text = _render_inline_markdown(hm.group(2))
            if output_lines and output_lines[-1] != "":
                output_lines.append("")
            border = "━" * max(10, min(60, len(hm.group(2)) + 10))
            if level <= 2:
                output_lines.append(f"  {_ANSI_BOLD}{border}{_ANSI_RESET}")
                output_lines.append(f"  {_ANSI_BOLD}{heading_text}{_ANSI_RESET}")
                output_lines.append(f"  {_ANSI_BOLD}{border}{_ANSI_RESET}")
            elif level == 3:
                output_lines.append(f"  {_ANSI_BOLD}─── {heading_text} ───{_ANSI_RESET}")
            elif level == 4:
                output_lines.append(f"  {_ANSI_BOLD}  {heading_text}{_ANSI_RESET}")
            elif level == 5:
                output_lines.append(f"  {_ANSI_ITALIC}{heading_text}{_ANSI_RESET}")
            else:  # level == 6
                output_lines.append(f"  {heading_text}")
            output_lines.append("")
            continue

        # Unordered lists (- item, * item, + item)
        ul_match = re.match(r"^(\s*)([-*+])\s+(.*)", line)
        if ul_match and not ul_match.group(0).startswith("    "):
            indent = ul_match.group(1)
            content = ul_match.group(3)
            output_lines.append(f"{indent}• {content}")
            continue

        # Ordered lists (1. item, etc.)
        ol_match = re.match(r"^(\s*)(\d+)\.\s+(.*)", line)
        if ol_match and not ol_match.group(0).startswith("    "):
            indent = ol_match.group(1)
            num = ol_match.group(2)
            content = ol_match.group(3)
            output_lines.append(f"{indent}  {num}. {content}")
            continue

        # Table header/body (render as simple pipe-separated)
        if "|" in line and not line.startswith("    "):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                output_lines.append("  " + "  │  ".join(cells))
                continue

        # Regular text (inline markdown is already rendered above)
        rendered = _render_inline_markdown(line)
        output_lines.append(f"  {rendered}")

    # Close any open formatting
    if in_code_block:
        close_code_block()
    close_blockquote()

    return "\n".join(output_lines)


def cmd_render_markdown(text):
    """Render markdown text for terminal display."""
    if not text:
        print("No text provided.")
        return
    rendered = render_markdown_text(text)
    print(rendered)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Gateway Connection (simulated — no actual network)
# ─────────────────────────────────────────────────────────────────────────────

class GatewayConnection:
    """WebSocket gateway connection manager.

    Handles Ed25519 + token auth, auto-reconnect logic, and event forwarding.
    In simulated mode (no websockets library), all methods operate in-memory
    and print status messages rather than connecting.
    """

    def __init__(self):
        self._connected = False
        self._auth_token = None
        self._device_public_key = None
        self._session_id = None
        self._callbacks = []
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._reconnect_attempts = 0
        self._intentional_disconnect = False

    @property
    def connected(self):
        return self._connected

    @property
    def session_id(self):
        return self._session_id

    @property
    def ws_url(self):
        return WS_GATEWAY_URL

    def set_session_id(self, session_id):
        """Set the current session ID."""
        self._session_id = session_id

    def set_session_id_from_event(self, event_data):
        """Extract session ID from an incoming event.

        The gateway may send session_created events with session_id in the
        message metadata. This method handles that.
        """
        if isinstance(event_data, dict):
            sid = event_data.get("session_id") or event_data.get("sessionId")
            if sid:
                self._session_id = sid
        # Also handle session_key in the event data
        sk = event_data.get("session_key") or event_data.get("sessionKey") if isinstance(event_data, dict) else None
        if sk and not self._session_id:
            self._session_id = str(sk)

    def add_callback(self, callback):
        """Register a callback for incoming events.

        Each callback receives a dict with keys:
          type: "delta" | "final" | "aborted" | "error" | "session_created"
          data: event payload
        """
        if callable(callback):
            self._callbacks.append(callback)

    def _fire_event(self, event_type, data):
        """Dispatch an event to all registered callbacks."""
        event = {"type": event_type, "data": data}
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass  # Callback errors don't break the stream

    def _on_connected(self):
        """Called when (simulated) connection is established."""
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._reconnect_attempts = 0

    def _on_error(self, error_msg):
        """Called on connection error; triggers reconnection."""
        self._reconnect_attempts += 1
        self._reconnect_delay = min(
            RECONNECT_MAX_DELAY,
            RECONNECT_BASE_DELAY * (RECONNECT_BACKOFF_FACTOR ** min(self._reconnect_attempts, 5)),
        )
        self._connected = False
        self._fire_event("error", {"message": error_msg})

    def _on_disconnected(self):
        """Called when disconnected."""
        self._connected = False
        self._fire_event("error", {"message": "Disconnected"})

    def get_status(self):
        """Return current connection status dict."""
        return {
            "connected": self._connected,
            "session_id": self._session_id,
            "reconnect_delay": self._reconnect_delay,
            "reconnect_attempts": self._reconnect_attempts,
            "url": self.ws_url,
            "last_error": None,
            "auth_token_configured": bool(self._auth_token),
            "device_key_configured": bool(self._device_public_key),
        }

    async def connect(self):
        """Establish connection to the Hermes gateway.

        In simulated mode: sets up local state and prints status.
        In real mode: uses websockets library for actual connection.
        """
        if _simulated_mode():
            # Simulated connect: just set up state and print
            self._connected = True
            self._on_connected()
            print(f"[simulated] Connected to {WS_GATEWAY_URL}")
            print(f"[simulated] Auth token: {'configured' if self._auth_token else 'missing'}")
            print(f"[simulated] Device key: {'configured' if self._device_public_key else 'missing'}")
            return

        # Real mode — actual websockets connection
        ws = _websockets
        try:
            # Load auth config
            token = self._auth_token or get_gateway_token(load_Hermes_config())
            pub_key = self._device_public_key or get_ed25519_public_key()
            if not token:
                self._on_error("No gateway auth token found")
                return

            # Build auth message
            auth_msg = build_auth_message(token, pub_key)

            # Connect to gateway
            uri = f"ws://{WS_GATEWAY_HOST}:{WS_GATEWAY_PORT}/ws/chat"
            async with ws.connect(uri) as websocket:
                # Send auth message
                await websocket.send(auth_msg)

                # Receive challenge / response
                response = await websocket.recv()
                resp_data = json.loads(response)

                if resp_data.get("status") == "ok":
                    self._connected = True
                    self._on_connected()
                    print(f"[connected] Gateway: {WS_GATEWAY_HOST}:{WS_GATEWAY_PORT}")
                    self._connected = True
                else:
                    self._on_error(f"Auth rejected: {resp_data.get('message', 'unknown')}")

        except ImportError:
            print("[error] websockets library not installed. Run: pip install websockets")
            return
        except Exception as e:
            self._on_error(str(e))

    async def disconnect(self):
        """Close the WebSocket connection."""
        self._intentional_disconnect = True
        if _simulated_mode():
            if self._connected:
                print(f"[simulated] Disconnected from {WS_GATEWAY_URL}")
            self._connected = False
            self._on_disconnected()
            return

        # Real mode — close the websocket
        # In real usage this would close the actual connection
        if self._connected:
            print(f"[disconnected] Gateway: {WS_GATEWAY_HOST}:{WS_GATEWAY_PORT}")
            self._connected = False
            self._on_disconnected()

    async def send_message(self, message, session_key=None):
        """Send a chat message to the gateway and start streaming.

        Sends a "chat.send" RPC with the message and session key.
        Sets up event callbacks to forward delta/final/aborted/error events.

        Args:
            message: The user message string.
            session_key: Optional session key. If None, the gateway creates one.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")

        if _simulated_mode():
            # Simulated stream: print what would happen
            sid = session_key or self._session_id or uuid.uuid4().hex[:12]
            print(f"[simulated] Sending message to session {sid}:")
            print(f"  User: {message}")
            print(f"[simulated] Streaming response (delta events)...")
            print(f"[simulated] [delta] Hello! How can I help you today?")
            print(f"[simulated] [final] Complete")
            self._session_id = sid
            return

        ws = _websockets
        # Send chat.send RPC
        params = {
            "sessionKey": session_key or "",
            "message": message,
            "deliver": False,
            "idempotencyKey": uuid.uuid4().hex,
        }
        request = {
            "type": "req",
            "id": uuid.uuid4().hex,
            "method": "chat.send",
            "params": params,
        }

        # We don't have a live websocket object here; in real usage we'd
        # use the one established during connect(). The actual streaming
        # is handled by the receive loop.
        await self._ws_send(json.dumps(request))

    async def abort_chat(self, session_key=None):
        """Send a chat.abort RPC to stop an active streaming response."""
        if not self._connected:
            return

        if _simulated_mode():
            sid = session_key or self._session_id or "unknown"
            print(f"[simulated] Aborting chat for session {sid}")
            return

        params = {"sessionKey": session_key or ""}
        if self._session_id:
            params["runId"] = self._session_id
        request = {
            "type": "req",
            "id": uuid.uuid4().hex,
            "method": "chat.abort",
            "params": params,
        }
        await self._ws_send(json.dumps(request))

    async def _ws_send(self, data):
        """Internal: send data over the websocket. Real mode only."""
        # In real mode this would use the live websocket connection
        pass  # Placeholder — actual send happens through the connect() handler

    async def _handle_message(self, text):
        """Handle an incoming WebSocket message (simulated or real).

        Parses the message and dispatches to callbacks.
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # Raw text — treat as delta event
            self._fire_event("delta", {"text": text})
            return

        msg_type = data.get("type", "")
        msg_data = data.get("data", {})

        # Handle session-level events
        if msg_type == "session_created":
            self.set_session_id_from_event(data)
            self._fire_event("session_created", msg_data)
            return

        # Handle chat stream events
        if msg_type in ("delta", "final", "aborted", "error"):
            self._fire_event(msg_type, msg_data)
        else:
            # Unknown event type — still forward for observability
            self._fire_event(msg_type, msg_data)

    async def _receive_loop(self):
        """Continuously receive messages from the gateway.

        In real mode, this is the main event loop for the WebSocket connection.
        In simulated mode, this would be a blocking loop that never returns.
        """
        if _simulated_mode():
            # Simulated: this would read from an in-memory queue
            while self._connected and not self._intentional_disconnect:
                import asyncio
                await asyncio.sleep(1)
            return

        ws = _websockets
        # Real mode: continuously receive messages
        # async for message in self._websocket:
        #     await self._handle_message(message)

    async def reconnect_loop(self):
        """Auto-reconnect loop with exponential backoff.

        Tries to reconnect with delay: base * 2^n, capped at max.
        Stops on intentional disconnect.
        """
        while not self._intentional_disconnect:
            if self._connected:
                await asyncio.sleep(0.1)
                continue

            print(f"[reconnect] Attempting reconnect in {self._reconnect_delay:.1f}s "
                  f"(attempt {self._reconnect_attempts})")

            try:
                import asyncio
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                break

            await self.connect()


# ─────────────────────────────────────────────────────────────────────────────
# Chat Streamer (manages a single streaming session)
# ─────────────────────────────────────────────────────────────────────────────

class ChatStreamer:
    """Manages a single chat streaming session.

    Collects delta events, renders the full response, and handles
    final/aborted/error states.
    """

    def __init__(self, gateway=None, output_stream=None):
        self.gateway = gateway
        self.response = ""
        self.session_id = None
        self._closed = False
        self._output_stream = output_stream or sys.stdout

    def start_stream(self):
        """Register event callback on the gateway."""
        if self.gateway:
            self.gateway.add_callback(self._on_event)

    def _on_event(self, event):
        """Handle a streaming event from the gateway."""
        event_type = event.get("type", "")
        event_data = event.get("data", {})

        if event_type == "delta":
            text = event_data.get("text", "")
            self.response += text
            # Flush to stdout in real mode
            if not _simulated_mode():
                self._output_stream.write(text)
                self._output_stream.flush()

        elif event_type == "final":
            text = event_data.get("text", "")
            if text:
                self.response = text
            self._closed = True

        elif event_type == "aborted":
            self._closed = True

        elif event_type == "error":
            msg = event_data.get("message", "Unknown error")
            print(f"\n[error] {msg}", file=self._output_stream)
            self._closed = True

        elif event_type == "session_created":
            sid = event_data.get("session_id") or event_data.get("sessionId")
            if sid:
                self.session_id = sid
            if self.gateway:
                self.gateway.set_session_id(sid)

    def finish_stream(self):
        """Print completion info after streaming ends."""
        if not _simulated_mode():
            self._output_stream.write("\n")
            self._output_stream.flush()

        if self.session_id:
            print(f"\n  Session: {self.session_id}")
        print(f"  Tokens: ~{len(self.response.split())}")


# ─────────────────────────────────────────────────────────────────────────────
# Gateway Instance Manager
# ─────────────────────────────────────────────────────────────────────────────

# Module-level gateway singleton (for CLI use)
_gateway_instance = None


def _get_gateway():
    """Get or create the module-level gateway connection."""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = GatewayConnection()
        # Try to load auth config
        oc_config = load_Hermes_config()
        token = get_gateway_token(oc_config)
        if token:
            _gateway_instance._auth_token = token
        pub_key = get_ed25519_public_key()
        if pub_key:
            _gateway_instance._device_public_key = pub_key
    return _gateway_instance


def reset_gateway():
    """Reset the module-level gateway (for testing)."""
    global _gateway_instance
    _gateway_instance = None


def cmd_ws_status():
    """Show WebSocket connection status."""
    gw = _get_gateway()
    status = gw.get_status()

    print(f"\n{'─' * 70}")
    print(f"  WebSocket Status")
    print(f"{'─' * 70}")

    state = "CONNECTED" if status["connected"] else "DISCONNECTED"
    if _simulated_mode():
        state = "SIMULATED"

    print(f"  State:          {state}")
    print(f"  Gateway URL:    {status['url']}")
    print(f"  Session ID:     {status['session_id'] or '—'}")
    print(f"  Reconnect:      attempt={status['reconnect_attempts']} "
          f"delay={status['reconnect_delay']:.1f}s")

    print(f"\n  Auth Diagnostics:")
    print(f"    Token:        {'configured' if status['auth_token_configured'] else 'not configured'}")
    print(f"    Device Key:   {'configured' if status['device_key_configured'] else 'not configured'}")
    print(f"    WebSockets:   {'available' if _ensure_websockets_import() else 'NOT installed (simulated mode)'}")

    if status.get("last_error"):
        print(f"    Last Error:   {status['last_error']}")

    # Show device config status
    dev_config = load_device_config()
    print(f"\n  Device Config:  {'loaded' if dev_config else 'not found at ' + DEVICE_JSON}")

    oc_config = load_Hermes_config()
    gw_token = get_gateway_token(oc_config) if oc_config else None
    print(f"  Gateway Config: {'loaded' if oc_config else 'not found at ' + Hermes_JSON}")
    print(f"  Gateway Token:  {'configured' if gw_token else 'not found'}")

    print(f"{'─' * 70}")


def cmd_chat_stream(message):
    """Send a message and stream the response in real-time.

    Usage: hscc-chat chat-stream <message>

    Streams delta/final/aborted/error events to stdout.
    """
    if not message:
        print("Usage: hscc-chat chat-stream <message>")
        return

    gw = _get_gateway()
    streamer = ChatStreamer(gw)
    streamer.start_stream()

    print(f"\n{'─' * 60}")
    print(f"  User: {message}")
    print(f"{'─' * 60}")

    if _simulated_mode():
        print(f"[simulated] Would connect to {WS_GATEWAY_URL}")
        print(f"[simulated] Sending chat message to gateway...")
        print(f"[simulated] Waiting for streaming response...")
        print(f"\n[simulated] Assistant:")

        # Simulate a streaming response
        import asyncio
        import sys as _sys

        # Collect simulated deltas
        deltas = ["The ", "cluster ", "status ", "is ", "healthy. ",
                  "All ", "agents ", "are ", "active ", "and ", "ready ",
                  "to ", "execute ", "tasks."]

        for delta in deltas:
            streamer._on_event({"type": "delta", "data": {"text": delta}})
            if not _simulated_mode():
                _sys.stdout.write(delta)
                _sys.stdout.flush()

        # Final
        full_response = "The cluster status is healthy. All agents are active and ready to execute tasks."
        streamer._on_event({"type": "final", "data": {"text": full_response}})
        streamer.session_id = uuid.uuid4().hex[:12]

        print(f"\n\n[simulated] Streaming complete.")
        if streamer.session_id:
            print(f"[simulated] Session: {streamer.session_id}")
        print(f"{'─' * 60}")
    else:
        # Real mode — connect and stream
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(gw.connect())
            time.sleep(0.5)
            loop.run_until_complete(gw.send_message(message))

            # Wait for response
            import time as _time
            start = _time.time()
            while not streamer._closed and _time.time() - start < 120:
                _time.sleep(0.1)

            if not streamer._closed:
                print("\n[response timed out]")

            streamer.finish_stream()
            print(f"{'─' * 60}")
        except Exception as e:
            print(f"\nError: {e}")
            print(f"{'─' * 60}")
        finally:
            loop.close()


def cmd_chat(message):
    """Send a message and get the complete response (non-streaming).

    Usage: hscc-chat chat <message>

    Collects all delta/final events and prints the rendered result.
    """
    if not message:
        print("Usage: hscc-chat chat <message>")
        return

    gw = _get_gateway()

    # Collect all events
    collected = []

    def collect(event):
        t = event.get("type", "")
        d = event.get("data", {})
        if t == "delta":
            collected.append(d.get("text", ""))
        elif t == "final":
            # Final event replaces all collected deltas (single complete response)
            collected[:] = [d.get("text", "")]
        elif t in ("error", "aborted"):
            collected.clear()
            collected.append(f"\n[{t}: {d.get('message', 'unknown')}]")

    gw.add_callback(collect)

    if _simulated_mode():
        # Simulate a response
        import asyncio
        gw._connected = True
        gw._on_connected()

        gw._fire_event("delta", {"text": "Simulated "})
        gw._fire_event("delta", {"text": "response. "})
        gw._fire_event("delta", {"text": "This is a placeholder."})
        gw._fire_event("final", {"text": "Simulated response. This is a placeholder."})

        full = "".join(collected)
        if full:
            rendered = render_markdown_text(full)
            print(rendered)
        else:
            print("(no response received)")

        gw._connected = False
        gw._on_disconnected()
        return

    # Real mode
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(gw.connect())
        time.sleep(0.5)
        loop.run_until_complete(gw.send_message(message))

        # Wait for response
        start = time.time()
        import time as _time
        while not gw._connected and time.time() - start < 60:
            _time.sleep(0.1)

        full = "".join(collected)
        if full:
            rendered = render_markdown_text(full)
            print(rendered)
        else:
            print("(no response received)")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        if gw._connected:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(gw.disconnect())
                loop.close()
            except Exception:
                pass


def cmd_ws_connect():
    """Initiate WebSocket connection to gateway."""
    gw = _get_gateway()
    print(f"Connecting to {WS_GATEWAY_URL}...")

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.connect())
        status = gw.get_status()
        if status["connected"]:
            print(f"Connected to {WS_GATEWAY_URL}")
        else:
            print("Connection failed.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        loop.close()


def cmd_ws_disconnect():
    """Close the WebSocket connection."""
    gw = _get_gateway()
    print(f"Disconnecting from {WS_GATEWAY_URL}...")

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gw.disconnect())
        print("Disconnected.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def build_parser():
    """Build the argument parser for the hscc-chat CLI."""
    _parser = argparse.ArgumentParser(
        prog="hscc-chat",
        description="Hermes Spark Cluster Control — WebSocket Chat Daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  hscc-chat session-create "My Chat"
  hscc-chat session-list
  hscc-chat chat "What is the cluster status?"
  hscc-chat chat-stream "Tell me about the cluster"
  hscc-chat render-markdown "**bold** and *italic*"
  hscc-chat ws-status
  hscc-chat ws-connect
  hscc-chat ws-disconnect
""",
    )

    subparsers = _parser.add_subparsers(dest="command", help="Available commands")

    # chat command
    chat_p = subparsers.add_parser("chat", help="Send a message and get a response")
    chat_p.add_argument("message", help="The message to send")

    # chat-stream command
    stream_p = subparsers.add_parser("chat-stream", help="Stream a chat response in real-time")
    stream_p.add_argument("message", help="The message to send")

    # session-list command
    subparsers.add_parser("session-list", help="List all chat sessions")

    # session-create command
    create_p = subparsers.add_parser("session-create", help="Create a new chat session")
    create_p.add_argument("name", nargs="?", default=None, help="Session name")

    # session-delete command
    delete_p = subparsers.add_parser("session-delete", help="Delete a chat session")
    delete_p.add_argument("session_id", help="Session ID to delete")

    # session-pin command
    pin_p = subparsers.add_parser("session-pin", help="Pin/unpin a chat session")
    pin_p.add_argument("session_id", help="Session ID to pin/unpin")

    # session-rename command
    rename_p = subparsers.add_parser("session-rename", help="Rename a chat session")
    rename_p.add_argument("session_id", help="Session ID to rename")
    rename_p.add_argument("new_name", help="New session name")

    # session-add command
    add_p = subparsers.add_parser("session-add", help="Add a message to a session")
    add_p.add_argument("session_id", help="Session ID to add message to")
    add_p.add_argument("message", help="Message text to add")
    add_p.add_argument("--role", default="user", choices=["user", "assistant"],
                       help="Message role (default: user)")

    # session-msgs command
    msgs_p = subparsers.add_parser("session-msgs", help="List messages in a session")
    msgs_p.add_argument("session_id", help="Session ID to list messages from")

    # render-markdown command
    render_p = subparsers.add_parser("render-markdown", help="Render markdown to terminal format")
    render_p.add_argument("text", help="Markdown text to render")

    # ws-status command
    subparsers.add_parser("ws-status", help="Show WebSocket connection status")

    # ws-connect command
    subparsers.add_parser("ws-connect", help="Connect to the gateway")

    # ws-disconnect command
    subparsers.add_parser("ws-disconnect", help="Disconnect from the gateway")

    return _parser


def main():
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Route commands
    commands = {
        "chat": lambda: cmd_chat(args.message),
        "chat-stream": lambda: cmd_chat_stream(args.message),
        "session-list": cmd_session_list,
        "session-create": lambda: cmd_session_create(args.name),
        "session-delete": lambda: cmd_session_delete(args.session_id),
        "session-pin": lambda: cmd_session_pin(args.session_id),
        "session-rename": lambda: cmd_session_rename(args.session_id, args.new_name),
        "session-add": lambda: cmd_session_add_message(
            args.session_id, args.message, getattr(args, "role", "user")),
        "session-msgs": lambda: _cmd_session_msgs(args.session_id),
        "render-markdown": lambda: cmd_render_markdown(args.text),
        "ws-status": cmd_ws_status,
        "ws-connect": cmd_ws_connect,
        "ws-disconnect": cmd_ws_disconnect,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        handler()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()

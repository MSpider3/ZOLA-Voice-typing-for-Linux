"""
injector.py — Keyboard Injection + Clipboard Integration
=========================================================
Primary output path: clipboard paste (wl-copy + Ctrl+V via uinput).
  → Works for ALL Unicode including Devanagari Hindi script.
  → Reliable on Wayland/Niri without per-compositor keymap issues.

Fallback output path: character-by-character uinput keystrokes.
  → US-QWERTY ASCII only (_CHAR_MAP).
  → Used only when clipboard paste fails (e.g., password fields that
    block paste, or when wl-copy/WAYLAND_DISPLAY is unavailable).

PREREQUISITES (one-time system setup):
  1. Create udev rule:
       echo 'KERNEL=="uinput", MODE="0660", GROUP="input"' | \\
       sudo tee /etc/udev/rules.d/99-uinput.rules
       sudo udevadm control --reload-rules && sudo udevadm trigger
  2. Add user to input group:
       sudo usermod -aG input $USER
  3. Log out and back in (or reboot) to apply group membership.

When running as a systemd user service, ensure Wayland env is imported:
  systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
"""

import asyncio
import logging
import os
import time

from evdev import UInput, ecodes

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Full US QWERTY Key Mapping (ASCII fallback only)
# --------------------------------------------------------------------------- #
# NOTE: This map is ASCII/Latin-script only.
# Devanagari (Hindi), CJK, Arabic, and other non-Latin scripts are NOT here.
# For those, the clipboard paste path (inject_text → _clipboard_paste) is used.
_CHAR_MAP: dict[str, tuple[int, bool]] = {
    # Lowercase letters
    "a": (ecodes.KEY_A, False), "b": (ecodes.KEY_B, False),
    "c": (ecodes.KEY_C, False), "d": (ecodes.KEY_D, False),
    "e": (ecodes.KEY_E, False), "f": (ecodes.KEY_F, False),
    "g": (ecodes.KEY_G, False), "h": (ecodes.KEY_H, False),
    "i": (ecodes.KEY_I, False), "j": (ecodes.KEY_J, False),
    "k": (ecodes.KEY_K, False), "l": (ecodes.KEY_L, False),
    "m": (ecodes.KEY_M, False), "n": (ecodes.KEY_N, False),
    "o": (ecodes.KEY_O, False), "p": (ecodes.KEY_P, False),
    "q": (ecodes.KEY_Q, False), "r": (ecodes.KEY_R, False),
    "s": (ecodes.KEY_S, False), "t": (ecodes.KEY_T, False),
    "u": (ecodes.KEY_U, False), "v": (ecodes.KEY_V, False),
    "w": (ecodes.KEY_W, False), "x": (ecodes.KEY_X, False),
    "y": (ecodes.KEY_Y, False), "z": (ecodes.KEY_Z, False),
    # Uppercase
    "A": (ecodes.KEY_A, True), "B": (ecodes.KEY_B, True),
    "C": (ecodes.KEY_C, True), "D": (ecodes.KEY_D, True),
    "E": (ecodes.KEY_E, True), "F": (ecodes.KEY_F, True),
    "G": (ecodes.KEY_G, True), "H": (ecodes.KEY_H, True),
    "I": (ecodes.KEY_I, True), "J": (ecodes.KEY_J, True),
    "K": (ecodes.KEY_K, True), "L": (ecodes.KEY_L, True),
    "M": (ecodes.KEY_M, True), "N": (ecodes.KEY_N, True),
    "O": (ecodes.KEY_O, True), "P": (ecodes.KEY_P, True),
    "Q": (ecodes.KEY_Q, True), "R": (ecodes.KEY_R, True),
    "S": (ecodes.KEY_S, True), "T": (ecodes.KEY_T, True),
    "U": (ecodes.KEY_U, True), "V": (ecodes.KEY_V, True),
    "W": (ecodes.KEY_W, True), "X": (ecodes.KEY_X, True),
    "Y": (ecodes.KEY_Y, True), "Z": (ecodes.KEY_Z, True),
    # Digits
    "0": (ecodes.KEY_0, False), "1": (ecodes.KEY_1, False),
    "2": (ecodes.KEY_2, False), "3": (ecodes.KEY_3, False),
    "4": (ecodes.KEY_4, False), "5": (ecodes.KEY_5, False),
    "6": (ecodes.KEY_6, False), "7": (ecodes.KEY_7, False),
    "8": (ecodes.KEY_8, False), "9": (ecodes.KEY_9, False),
    # Shifted digit symbols
    "!": (ecodes.KEY_1, True),  "@": (ecodes.KEY_2, True),
    "#": (ecodes.KEY_3, True),  "$": (ecodes.KEY_4, True),
    "%": (ecodes.KEY_5, True),  "^": (ecodes.KEY_6, True),
    "&": (ecodes.KEY_7, True),  "*": (ecodes.KEY_8, True),
    "(": (ecodes.KEY_9, True),  ")": (ecodes.KEY_0, True),
    # Punctuation (unshifted)
    " ": (ecodes.KEY_SPACE, False),
    "\n": (ecodes.KEY_ENTER, False),
    "\t": (ecodes.KEY_TAB, False),
    ".": (ecodes.KEY_DOT, False),
    ",": (ecodes.KEY_COMMA, False),
    "/": (ecodes.KEY_SLASH, False),
    ";": (ecodes.KEY_SEMICOLON, False),
    "'": (ecodes.KEY_APOSTROPHE, False),
    "[": (ecodes.KEY_LEFTBRACE, False),
    "]": (ecodes.KEY_RIGHTBRACE, False),
    "\\": (ecodes.KEY_BACKSLASH, False),
    "`": (ecodes.KEY_GRAVE, False),
    "-": (ecodes.KEY_MINUS, False),
    "=": (ecodes.KEY_EQUAL, False),
    # Punctuation (shifted)
    ">": (ecodes.KEY_DOT, True),
    "<": (ecodes.KEY_COMMA, True),
    "?": (ecodes.KEY_SLASH, True),
    ":": (ecodes.KEY_SEMICOLON, True),
    '"': (ecodes.KEY_APOSTROPHE, True),
    "{": (ecodes.KEY_LEFTBRACE, True),
    "}": (ecodes.KEY_RIGHTBRACE, True),
    "|": (ecodes.KEY_BACKSLASH, True),
    "~": (ecodes.KEY_GRAVE, True),
    "_": (ecodes.KEY_MINUS, True),
    "+": (ecodes.KEY_EQUAL, True),
}

_ALL_KEYS = list({keycode for keycode, _ in _CHAR_MAP.values()} | {
    ecodes.KEY_LEFTSHIFT,
    ecodes.KEY_LEFTCTRL,
    ecodes.KEY_V,
    ecodes.KEY_INSERT,
    ecodes.KEY_BACKSPACE,
    ecodes.KEY_SPACE,
    ecodes.KEY_ENTER,
    ecodes.KEY_TAB,
})


# Paste key combos (paste_method setting): modifiers held, then the key
_PASTE_KEYS = {
    "ctrl_v": ([ecodes.KEY_LEFTCTRL], ecodes.KEY_V),
    "ctrl_shift_v": ([ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT], ecodes.KEY_V),
    "shift_insert": ([ecodes.KEY_LEFTSHIFT], ecodes.KEY_INSERT),
}

# How long the pasted text stays in the clipboard before the previous
# content is restored; the target app reads it asynchronously after the keys.
_RESTORE_DELAY_S = 0.5


def _text_is_ascii_only(text: str) -> bool:
    """Return True if text contains only characters in _CHAR_MAP (pure ASCII/Latin)."""
    return all(c in _CHAR_MAP for c in text)


class KeystrokeInjector:
    """
    Text injection driver for Wayland.

    inject_text() is the main entry point. It tries clipboard paste first
    (works for ALL Unicode, including Devanagari Hindi) and falls back to
    uinput character-by-character only for pure ASCII text when paste fails.

    type_text() is synchronous and must be run in a
    ThreadPoolExecutor. They contain time.sleep() calls for compositor stability.

    copy_to_clipboard() and inject_text() are async (use asyncio subprocess).
    """

    def __init__(self) -> None:
        self._wayland_env = _capture_wayland_env()

        try:
            capabilities = {ecodes.EV_KEY: _ALL_KEYS}
            self._ui = UInput(capabilities, name="zola-virtual-keyboard", version=0x3)
            logger.info(
                "KeystrokeInjector: UInput device created ('%s'), "
                "WAYLAND_DISPLAY=%s, XDG_RUNTIME_DIR=%s",
                self._ui.name,
                self._wayland_env.get("WAYLAND_DISPLAY", "MISSING"),
                self._wayland_env.get("XDG_RUNTIME_DIR", "MISSING"),
            )
        except PermissionError:
            logger.critical(
                "KeystrokeInjector: cannot open /dev/uinput — PermissionError.\n"
                "Run the following one-time setup commands:\n"
                "  echo 'KERNEL==\"uinput\", MODE=\"0660\", GROUP=\"input\"' | "
                "sudo tee /etc/udev/rules.d/99-uinput.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                "  sudo usermod -aG input $USER\n"
                "  # Then log out and back in (or reboot)"
            )
            raise
        except Exception as exc:
            logger.exception("KeystrokeInjector: failed to create UInput device: %s", exc)
            raise

    def _missing_wayland_env(self) -> list[str]:
        """
        Names of the Wayland env vars still missing. Re-probes first, because
        the daemon may have started before the compositor created its socket.
        """
        keys = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        if not all(self._wayland_env.get(k) for k in keys):
            self._wayland_env = _capture_wayland_env()
        return [k for k in keys if not self._wayland_env.get(k)]

    # ----------------------------------------------------------------------- #
    # Primary injection path — clipboard paste (Unicode-safe)
    # ----------------------------------------------------------------------- #
    async def inject_text(
        self,
        text: str,
        delay_ms: int = 12,
        paste_method: str = "ctrl_v",
        restore_clipboard: bool = False,
    ) -> None:
        """
        Main entry point for injecting transcribed text into the focused app.

        Strategy:
          1. Try wl-copy + Ctrl+V paste (works for all Unicode, including Hindi).
          2. If paste fails (wl-copy missing, WAYLAND_DISPLAY not set, or the
             target app blocks Ctrl+V), fall back to uinput character-by-character.
             The fallback silently skips any non-ASCII characters.

        This means Hindi Devanagari output from Whisper (अ, आ, ि…) works
        correctly via paste, whereas the old uinput-only path would silently
        drop every Devanagari character.
        """
        if not text:
            return

        # Attempt 1: clipboard paste (primary — Unicode-safe)
        previous = await self._read_clipboard() if restore_clipboard else None
        paste_ok = await self._clipboard_paste(text, delay_ms, paste_method)
        if paste_ok:
            if previous is not None:
                await asyncio.sleep(_RESTORE_DELAY_S)
                await self._wl_copy(previous)
            return

        # Attempt 2: uinput fallback (ASCII only — logs skipped non-ASCII chars)
        if not _text_is_ascii_only(text):
            logger.warning(
                "KeystrokeInjector.inject_text: clipboard paste failed and text contains "
                "non-ASCII characters (%d chars). Non-ASCII will be skipped in uinput fallback. "
                "Ensure wl-clipboard is installed and WAYLAND_DISPLAY is set.",
                len(text),
            )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.type_text, text, delay_ms)

    async def _read_clipboard(self) -> str | None:
        """Current clipboard text, or None if empty / not text / unavailable."""
        if self._missing_wayland_env():
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8",
                env=self._wayland_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return out.decode("utf-8") if proc.returncode == 0 else None
        except (FileNotFoundError, asyncio.TimeoutError, UnicodeDecodeError):
            return None

    async def _wl_copy(self, text: str, primary: bool = False) -> bool:
        """Put text into the clipboard (or the primary selection). True on success."""
        # Text goes on stdin: a command-line argument would be visible to every
        # local process (ps, /proc/<pid>/cmdline) for as long as wl-copy serves it.
        # stderr must not be a pipe: wl-copy forks a server that keeps it open, so
        # waiting for it hung every paste for the full timeout (then fell back to
        # ASCII-only typing).
        args = ["wl-copy"] + (["--primary"] if primary else [])
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=self._wayland_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=5.0)
        if proc.returncode != 0:
            logger.error("KeystrokeInjector: wl-copy exited %d", proc.returncode)
            return False
        return True

    async def _clipboard_paste(self, text: str, delay_ms: int = 12, paste_method: str = "ctrl_v") -> bool:
        """
        Copy text to Wayland clipboard with wl-copy, then send the paste keys.
        shift_insert also fills the primary selection, which terminals paste.
        Returns True on success, False on any failure (caller should fallback).
        """
        missing_env = self._missing_wayland_env()
        if missing_env:
            logger.debug(
                "KeystrokeInjector._clipboard_paste: missing env %s — skipping paste path",
                missing_env,
            )
            return False

        try:
            # Step 1: write to clipboard
            if not await self._wl_copy(text):
                return False
            if paste_method == "shift_insert" and not await self._wl_copy(text, primary=True):
                return False

            # Step 2: send the paste keys to the focused window
            # Small settle delay so the compositor processes the clipboard write
            await asyncio.sleep(0.05)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_paste, paste_method, delay_ms)

            logger.debug(
                "KeystrokeInjector._clipboard_paste: pasted %d chars via wl-copy+Ctrl+V",
                len(text),
            )
            return True

        except FileNotFoundError:
            logger.error(
                "KeystrokeInjector._clipboard_paste: 'wl-copy' not found. "
                "Install with: sudo dnf install wl-clipboard"
            )
            return False
        except asyncio.TimeoutError:
            logger.error("KeystrokeInjector._clipboard_paste: wl-copy timed out after 5s")
            return False
        except Exception as exc:
            logger.exception("KeystrokeInjector._clipboard_paste: unexpected error: %s", exc)
            return False

    def _send_paste(self, paste_method: str = "ctrl_v", delay_ms: int = 12) -> None:
        """Send the paste key combo via uinput to trigger paste in the focused app."""
        delay_s = delay_ms / 1000.0
        modifiers, key = _PASTE_KEYS.get(paste_method, _PASTE_KEYS["ctrl_v"])
        try:
            for mod in modifiers:
                self._ui.write(ecodes.EV_KEY, mod, 1)
            self._ui.write(ecodes.EV_KEY, key, 1)
            self._ui.write(ecodes.EV_KEY, key, 0)
            for mod in reversed(modifiers):
                self._ui.write(ecodes.EV_KEY, mod, 0)
            self._ui.syn()
            time.sleep(delay_s)
        except Exception as exc:
            logger.error("KeystrokeInjector._send_paste: error: %s", exc)
            self.release_all()

    # ----------------------------------------------------------------------- #
    # ASCII fallback path — character-by-character uinput
    # ----------------------------------------------------------------------- #
    def type_text(self, text: str, delay_ms: int = 12) -> None:
        """
        Simulate typing text character by character via kernel UInput events.
        ASCII/Latin only. Non-ASCII characters are silently skipped with a warning.
        This is the FALLBACK path — inject_text() tries clipboard paste first.
        """
        if not text:
            return

        delay_s = delay_ms / 1000.0
        skipped = []

        try:
            for char in text:
                entry = _CHAR_MAP.get(char)
                if entry is None:
                    skipped.append(repr(char))
                    continue

                keycode, needs_shift = entry

                try:
                    if needs_shift:
                        self._ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                    self._ui.write(ecodes.EV_KEY, keycode, 1)
                    self._ui.write(ecodes.EV_KEY, keycode, 0)
                    if needs_shift:
                        self._ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                    self._ui.syn()
                    time.sleep(delay_s)
                except Exception as exc:
                    logger.error(
                        "KeystrokeInjector.type_text: error emitting char %r: %s", char, exc
                    )
                    self.release_all()
                    raise
        except Exception:
            self.release_all()
            raise

        if skipped:
            logger.warning(
                "KeystrokeInjector.type_text: %d character(s) skipped (not in keymap): %s",
                len(skipped), ", ".join(skipped[:10]),
            )

    # ----------------------------------------------------------------------- #
    # Clipboard-only copy (for session-end clipboard update, not injection)
    # ----------------------------------------------------------------------- #
    async def copy_to_clipboard(self, text: str) -> None:
        """
        Store text in the Wayland clipboard using wl-copy (no paste).
        Used at session end so the full transcript is available for manual paste.
        """
        if not text:
            return

        missing = self._missing_wayland_env()
        if missing:
            logger.warning(
                "KeystrokeInjector.copy_to_clipboard: missing env vars %s", missing
            )

        try:
            if await self._wl_copy(text):
                logger.debug(
                    "KeystrokeInjector.copy_to_clipboard: %d chars in clipboard", len(text)
                )
        except FileNotFoundError:
            logger.error(
                "KeystrokeInjector.copy_to_clipboard: 'wl-copy' not found. "
                "Install with: sudo dnf install wl-clipboard"
            )
        except asyncio.TimeoutError:
            logger.error("KeystrokeInjector.copy_to_clipboard: wl-copy timed out after 5s")
        except Exception as exc:
            logger.exception("KeystrokeInjector.copy_to_clipboard: unexpected error: %s", exc)

    async def send_notification(self, title: str, message: str) -> None:
        """Send a desktop notification using notify-send."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, message,
                env=self._wayland_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip() if stderr else "(no stderr)"
                logger.error(
                    "KeystrokeInjector.send_notification: notify-send exited %d: %s",
                    proc.returncode, err,
                )
        except FileNotFoundError:
            logger.error("KeystrokeInjector.send_notification: 'notify-send' not found.")
        except asyncio.TimeoutError:
            logger.error("KeystrokeInjector.send_notification: notify-send timed out")
        except Exception as exc:
            logger.exception("KeystrokeInjector.send_notification: unexpected error: %s", exc)

    # ----------------------------------------------------------------------- #
    # Cleanup
    # ----------------------------------------------------------------------- #
    def release_all(self) -> None:
        """Forcefully release all registered keys to prevent stuck modifiers."""
        if not hasattr(self, "_ui") or self._ui is None:
            return
        try:
            for keycode in _ALL_KEYS:
                self._ui.write(ecodes.EV_KEY, keycode, 0)
            self._ui.syn()
        except Exception as exc:
            logger.error("KeystrokeInjector.release_all failed: %s", exc)

    def close(self) -> None:
        """Release the UInput device. Called during daemon shutdown."""
        try:
            self.release_all()
            if hasattr(self, "_ui") and self._ui is not None:
                self._ui.close()
            logger.info("KeystrokeInjector: UInput device closed")
        except Exception as exc:
            logger.error("KeystrokeInjector.close: error: %s", exc)


# --------------------------------------------------------------------------- #
# Private Helpers
# --------------------------------------------------------------------------- #
def _capture_wayland_env() -> dict[str, str]:
    """
    Build the minimal environment dict needed for Wayland subprocess calls.
    Probes XDG_RUNTIME_DIR and WAYLAND_DISPLAY if missing from environment.
    """
    env = dict(os.environ)

    if "XDG_RUNTIME_DIR" not in env:
        uid = os.getuid()
        xdg_fallback = f"/run/user/{uid}"
        if os.path.isdir(xdg_fallback):
            env["XDG_RUNTIME_DIR"] = xdg_fallback
            logger.info("injector: probed XDG_RUNTIME_DIR=%s", xdg_fallback)
        else:
            logger.warning(
                "injector: XDG_RUNTIME_DIR not found and %s does not exist. "
                "wl-copy clipboard integration will not work. "
                "Run: systemctl --user import-environment XDG_RUNTIME_DIR",
                xdg_fallback,
            )

    if "WAYLAND_DISPLAY" not in env:
        runtime = env.get("XDG_RUNTIME_DIR", "")
        probed = False
        if runtime and os.path.isdir(runtime):
            from pathlib import Path as _Path
            for sock in sorted(_Path(runtime).glob("wayland-*")):
                try:
                    if sock.is_socket():
                        env["WAYLAND_DISPLAY"] = sock.name
                        logger.info("injector: probed WAYLAND_DISPLAY=%s", sock.name)
                        probed = True
                        break
                except OSError:
                    continue
        if not probed:
            logger.warning(
                "injector: WAYLAND_DISPLAY not found and no wayland-* socket in "
                "XDG_RUNTIME_DIR. Run: systemctl --user import-environment WAYLAND_DISPLAY"
            )

    return env

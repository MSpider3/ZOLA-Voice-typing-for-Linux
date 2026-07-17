"""
injector.py — Kernel-Level Keystroke Injection + Clipboard Integration
=======================================================================
Uses python-evdev's UInput interface to create a virtual keyboard device
registered at the kernel level via /dev/uinput. Because this operates at
the kernel input layer, Wayland compositors (including Niri) treat events
as coming from real hardware — bypassing Wayland's application-isolation
security model for synthetic input.

PREREQUISITES (one-time system setup, NOT handled by this module):
  1. Create udev rule:
       echo 'KERNEL=="uinput", MODE="0660", GROUP="input"' | \
       sudo tee /etc/udev/rules.d/99-uinput.rules
       sudo udevadm control --reload-rules && sudo udevadm trigger
  2. Add user to input group:
       sudo usermod -aG input $USER
  3. Log out and back in (or reboot) to apply group membership.

Wayland clipboard (wl-copy) requires WAYLAND_DISPLAY and XDG_RUNTIME_DIR.
These are captured from os.environ at init time. When running as a systemd
user service, ensure these are imported:
  systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
"""

import asyncio
import logging
import os
import time
from typing import Optional

import evdev
from evdev import UInput, ecodes

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Full US QWERTY Key Mapping
# --------------------------------------------------------------------------- #
# Maps printable ASCII characters to (evdev keycode, requires_shift).
# This covers all characters that appear in English + common punctuation.
# Hinglish text (Latin-script Devanagari transliteration) uses the same set.
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
    # Uppercase letters (shift + key)
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

# Complete set of keycodes needed to declare to the kernel
_ALL_KEYS = list({keycode for keycode, _ in _CHAR_MAP.values()} | {
    ecodes.KEY_LEFTSHIFT,
    ecodes.KEY_BACKSPACE,
    ecodes.KEY_SPACE,
    ecodes.KEY_ENTER,
    ecodes.KEY_TAB,
})


class KeystrokeInjector:
    """
    Virtual keyboard driver using evdev UInput.

    type_text() and erase_chars() are synchronous and must be run in a
    ThreadPoolExecutor from async code. They contain time.sleep() calls
    for compositor stability which would block the event loop if awaited.

    copy_to_clipboard() is async because wl-copy is invoked as a subprocess
    via asyncio.create_subprocess_exec().
    """

    def __init__(self) -> None:
        # Capture Wayland environment at init time (from the user session that
        # launched the daemon). These are required by wl-copy.
        self._wayland_env = _capture_wayland_env()

        # Create the virtual keyboard device
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

    # ----------------------------------------------------------------------- #
    # Keystroke Injection (synchronous — run in ThreadPoolExecutor)
    # ----------------------------------------------------------------------- #
    def type_text(self, text: str, delay_ms: int = 12) -> None:
        """
        Simulate typing `text` character by character via kernel UInput events.
        Each character is broken into press → release (+ shift if needed).

        Args:
            text:     The string to type.
            delay_ms: Inter-key delay in milliseconds. 12ms is the minimum
                      that Niri needs to process events reliably. Increase
                      to 20-30ms for applications with slow input handlers.
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
                    self._ui.write(ecodes.EV_KEY, keycode, 1)   # key down
                    self._ui.write(ecodes.EV_KEY, keycode, 0)   # key up
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
                len(skipped),
                ", ".join(skipped),
            )

    def erase_chars(self, count: int, delay_ms: int = 8) -> None:
        """
        Emit `count` KEY_BACKSPACE events to erase previously typed text.
        Used by the realtime backspace-diff correction mechanism.

        Args:
            count:    Number of characters to erase.
            delay_ms: Inter-key delay. Can be slightly faster than type_text
                      since backspace doesn't trigger shift sequences.
        """
        if count <= 0:
            return

        delay_s = delay_ms / 1000.0
        logger.debug("KeystrokeInjector.erase_chars: erasing %d character(s)", count)

        try:
            for _ in range(count):
                self._ui.write(ecodes.EV_KEY, ecodes.KEY_BACKSPACE, 1)
                self._ui.write(ecodes.EV_KEY, ecodes.KEY_BACKSPACE, 0)
                self._ui.syn()
                time.sleep(delay_s)
        except Exception as exc:
            logger.error("KeystrokeInjector.erase_chars: error emitting backspace: %s", exc)
            self.release_all()

    # ----------------------------------------------------------------------- #
    # Clipboard Integration (async — uses asyncio subprocess)
    # ----------------------------------------------------------------------- #
    async def copy_to_clipboard(self, text: str) -> None:
        """
        Store `text` in the Wayland clipboard using wl-copy.
        Runs concurrently with keystroke injection (fire-and-forget).

        WAYLAND_DISPLAY and XDG_RUNTIME_DIR are injected explicitly into the
        subprocess environment so this works from systemd user services that
        may not inherit the full session environment.
        """
        if not text:
            return

        missing = [k for k in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
                   if not self._wayland_env.get(k)]
        if missing:
            logger.warning(
                "KeystrokeInjector.copy_to_clipboard: missing env vars %s — "
                "wl-copy may fail. Run: systemctl --user import-environment "
                "WAYLAND_DISPLAY XDG_RUNTIME_DIR",
                missing,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "wl-copy", "--", text,
                env=self._wayland_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() if stderr else "(no stderr)"
                logger.error(
                    "KeystrokeInjector.copy_to_clipboard: wl-copy exited %d: %s",
                    proc.returncode,
                    err_msg,
                )
            else:
                logger.debug(
                    "KeystrokeInjector.copy_to_clipboard: %d chars copied to clipboard",
                    len(text),
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
        """
        Send a desktop notification using notify-send.
        Uses the captured Wayland environment to communicate with DBus.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, message,
                env=self._wayland_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() if stderr else "(no stderr)"
                logger.error(
                    "KeystrokeInjector.send_notification: notify-send exited %d: %s",
                    proc.returncode,
                    err_msg,
                )
            else:
                logger.debug("KeystrokeInjector.send_notification: desktop notification sent")
        except FileNotFoundError:
            logger.error(
                "KeystrokeInjector.send_notification: 'notify-send' not found. "
                "Install with your package manager (e.g. libnotify or libnotify-bin)."
            )
        except asyncio.TimeoutError:
            logger.error("KeystrokeInjector.send_notification: notify-send timed out after 5s")
        except Exception as exc:
            logger.exception("KeystrokeInjector.send_notification: unexpected error: %s", exc)

    # ----------------------------------------------------------------------- #
    # Cleanup
    # ----------------------------------------------------------------------- #
    def release_all(self) -> None:
        """
        Forcefully release all registered keys (especially modifier keys like KEY_LEFTSHIFT)
        to prevent the operating system from getting stuck keys during a crash or shutdown.
        """
        if not hasattr(self, "_ui") or self._ui is None:
            return
        try:
            logger.info("KeystrokeInjector: releasing all virtual keys")
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
    Starts from the full current environment (so PATH, HOME, etc. are correct)
    and verifies the two critical Wayland variables are present.

    Fallback probe: if XDG_RUNTIME_DIR or WAYLAND_DISPLAY are missing (common
    in systemd --user services where import-environment was not called), probe
    the standard /run/user/<uid>/ XDG directory for Wayland socket files
    (wayland-0, wayland-1, etc.).
    """
    env = dict(os.environ)

    # --- XDG_RUNTIME_DIR fallback ---
    if "XDG_RUNTIME_DIR" not in env:
        uid = os.getuid()
        xdg_fallback = f"/run/user/{uid}"
        if os.path.isdir(xdg_fallback):
            env["XDG_RUNTIME_DIR"] = xdg_fallback
            logger.info(
                "injector: probed XDG_RUNTIME_DIR=%s (was missing from environment)",
                xdg_fallback,
            )
        else:
            logger.warning(
                "injector: 'XDG_RUNTIME_DIR' not found in environment and "
                "%s does not exist. wl-copy clipboard integration will not work. "
                "If running as a systemd service, run: "
                "systemctl --user import-environment XDG_RUNTIME_DIR",
                xdg_fallback,
            )

    # --- WAYLAND_DISPLAY fallback ---
    if "WAYLAND_DISPLAY" not in env:
        runtime = env.get("XDG_RUNTIME_DIR", "")
        probed = False
        if runtime and os.path.isdir(runtime):
            from pathlib import Path as _Path
            # Look for wayland-0, wayland-1, etc. (standard Wayland socket names)
            for sock in sorted(_Path(runtime).glob("wayland-*")):
                try:
                    if sock.is_socket():
                        env["WAYLAND_DISPLAY"] = sock.name  # e.g. "wayland-0"
                        logger.info(
                            "injector: probed WAYLAND_DISPLAY=%s from %s",
                            sock.name, runtime,
                        )
                        probed = True
                        break
                except OSError:
                    continue
        if not probed:
            logger.warning(
                "injector: 'WAYLAND_DISPLAY' not found in environment and no "
                "wayland-* socket found in XDG_RUNTIME_DIR. "
                "wl-copy clipboard integration will not work. "
                "If running as a systemd service, run: "
                "systemctl --user import-environment WAYLAND_DISPLAY",
            )

    return env

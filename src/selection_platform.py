"""Read highlighted text from other applications without starting a translation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

import platform_support

logger = logging.getLogger("clickntranslate.selection")


def read_primary_selection(app=None, *, use_qt=True):
    """Linux/X11 PRIMARY selection, or Qt selection when an app instance exists."""
    try:
        if app is None and use_qt:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
        if app is not None:
            clipboard = app.clipboard()
            text = clipboard.text(clipboard.Selection)
            if text and str(text).strip():
                return str(text).strip()
    except Exception:
        logger.debug("Qt PRIMARY selection read failed", exc_info=True)
    for command in (["xclip", "-o", "-selection", "primary"], ["xsel", "-p"]):
        if not shutil.which(command[0]):
            continue
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                env=platform_support.system_subprocess_env(),
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def read_selected_text(*, primary_text=None):
    """Selected text in the foreground application, without synthesizing keys."""
    if platform_support.IS_LINUX:
        if primary_text is not None:
            return primary_text.strip() or read_primary_selection(use_qt=False)
        return read_primary_selection()
    if platform_support.IS_MAC:
        from macos_desktop import read_foreground_selected_text

        return read_foreground_selected_text()
    if platform_support.IS_WINDOWS:
        from windows_selection import read_windows_selected_text

        return read_windows_selected_text()
    return ""


def foreground_token():
    """Identity of the foreground owner, for rejecting a stale asynchronous read."""
    if platform_support.IS_WINDOWS:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if platform_support.IS_MAC:
        import AppKit
        application = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(application.processIdentifier()) if application is not None else 0
    return None


def is_foreign_foreground():
    """True when another application owns the foreground window."""
    if platform_support.IS_WINDOWS:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return False
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) != os.getpid()
    if platform_support.IS_MAC:
        import AppKit

        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return app is not None and int(app.processIdentifier()) != os.getpid()
    from PyQt5.QtWidgets import QApplication
    return QApplication.activeWindow() is None

"""Print the UI Automation selection behind the foreground Windows application.

Select text with the mouse in Notepad, Chrome, or any other program. After the
left button comes up, the focused element, its parents, and the text range are
printed. Leave this window in the background. Press Ctrl+C here to stop.

    python tools/debug_windows_selection.py
"""

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import platform_support  # noqa: E402
import selection_platform  # noqa: E402
import windows_selection  # noqa: E402

_LEFT_BUTTON = 0x01


def main():
    if not platform_support.IS_WINDOWS:
        print("UI Automation selection exists only on Windows. Run this probe there.")
        return 2
    import ctypes

    windows_selection.activate_selection_access()
    print("Listening. Select text in Notepad or Chrome and release the mouse there.")
    print("The report names that window. A PyCharm title means the editor was in front, not the target app.")
    print("Press Ctrl+C in this window to stop.")
    button_down = False
    try:
        while True:
            pressed = bool(ctypes.windll.user32.GetAsyncKeyState(_LEFT_BUTTON) & 0x8000)
            released = button_down and not pressed
            button_down = pressed
            if released:
                hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
                if hwnd and selection_platform.is_foreign_foreground():
                    time.sleep(0.45)
                    print()
                    try:
                        print(windows_selection.describe_captured_window(hwnd), flush=True)
                    except Exception:
                        traceback.print_exc()
                    print("-" * 60, flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        windows_selection.deactivate_selection_access()


if __name__ == "__main__":
    sys.exit(main())

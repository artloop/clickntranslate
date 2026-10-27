"""Read selected text from the foreground Windows control via UI Automation."""

from __future__ import annotations

import logging

logger = logging.getLogger("clickntranslate.selection")


_TEXT_PATTERN_ID = 10014  # UIA_TextPatternId
_MAX_ANCESTORS = 12


def read_windows_selected_text():
    try:
        return _read_via_comtypes()
    except ImportError:
        logger.debug("UI Automation unavailable (comtypes not installed)")
    except Exception:
        logger.debug("UI Automation selection read failed", exc_info=True)
    return ""


def _selected_text_from_element(element):
    """Highlighted text exposed by one element's TextPattern, if any."""
    try:
        text_pattern = element.GetCurrentPattern(_TEXT_PATTERN_ID)
        if text_pattern is None:
            return ""
        ranges = text_pattern.GetSelection()
        if ranges is None or int(getattr(ranges, "Length", 0) or 0) <= 0:
            return ""
        parts = []
        for index in range(int(ranges.Length)):
            parts.append(str(ranges.GetElement(index).GetText(-1) or ""))
        return "".join(parts).strip()
    except Exception:
        logger.debug("TextPattern selection read failed", exc_info=True)
        return ""


def _selection_along_ancestors(element, parent_of, *, limit=_MAX_ANCESTORS):
    """Read the focused control, then its parents, without sending any input."""
    seen = 0
    while element is not None and seen <= limit:
        text = _selected_text_from_element(element)
        if text:
            return text
        seen += 1
        try:
            element = parent_of(element)
        except Exception:
            logger.debug("UI Automation parent walk failed", exc_info=True)
            return ""
    return ""


def _read_via_comtypes():
    import comtypes.client

    automation = comtypes.client.CreateObject("UIAutomationClient.CUIAutomation")
    element = automation.GetFocusedElement()
    if element is None:
        return ""
    try:
        walker = automation.ControlViewWalker
    except Exception:
        logger.debug("UI Automation control walker unavailable", exc_info=True)
        return _selected_text_from_element(element)

    def parent_of(current):
        return walker.GetParentElement(current)

    return _selection_along_ancestors(element, parent_of)

"""Read highlighted text from the foreground Windows control via UI Automation.

Notepad exposes the highlight as a TextPattern range on its document. Chrome
exposes it on the text container of the selected run. The whole document or
text-node name is a different property and is never treated as the selection.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("clickntranslate.selection")


_TEXT_PATTERN_ID = 10014  # UIA_TextPatternId
_TEXT_CHILD_PATTERN_ID = 10029  # UIA_TextChildPatternId
_MAX_ANCESTORS = 12
_AUTOMATION_PROGID = "UIAutomationClient.CUIAutomation"

_uia_module = None
_uia_module_checked = False
_listener = None
_listener_lock = threading.Lock()


def read_windows_selected_text():
    try:
        return _read_via_comtypes()
    except ImportError:
        logger.debug("UI Automation unavailable (comtypes not installed)")
    except Exception:
        logger.debug("UI Automation selection read failed", exc_info=True)
    return ""


def activate_selection_access():
    """Keep one UI Automation listener so providers publish their text tree.

    Chrome builds the tree inspected by Accessibility Insights only while a
    client is listening for events. Notepad publishes its document either way.
    """
    global _listener
    with _listener_lock:
        if _listener is not None:
            return
        listener = _SelectionAccess()
        try:
            listener.start()
        except Exception:
            logger.debug("UI Automation selection listener was not started", exc_info=True)
            return
        _listener = listener


def deactivate_selection_access():
    global _listener
    with _listener_lock:
        listener, _listener = _listener, None
    if listener is not None:
        listener.stop()


def _iter_ranges(selection):
    """Yield text ranges from the shapes UI Automation and tests return."""
    if selection is None:
        return
    if isinstance(selection, (list, tuple)):
        for item in selection:
            if item is not None:
                yield item
        return
    length = getattr(selection, "Length", None)
    get_element = getattr(selection, "GetElement", None)
    if length is not None and callable(get_element):
        for index in range(int(length or 0)):
            item = get_element(index)
            if item is not None:
                yield item
        return
    if callable(getattr(selection, "GetText", None)):
        yield selection


def _range_text(text_range):
    getter = getattr(text_range, "GetText", None)
    if not callable(getter):
        return ""
    try:
        value = getter(-1)
    except Exception:
        logger.debug("Text range read failed", exc_info=True)
        return ""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else ""
    return str(value or "")


def _text_of_selection(selection):
    return "".join(_range_text(text_range) for text_range in _iter_ranges(selection)).strip()


def _pattern(element, pattern_id, interface_name):
    getter = getattr(element, "GetCurrentPattern", None)
    if not callable(getter):
        return None
    try:
        pattern = getter(pattern_id)
    except Exception:
        return None
    if pattern is None:
        return None
    query = getattr(pattern, "QueryInterface", None)
    if callable(query):
        interface = _uia_interface(interface_name)
        if interface is not None:
            try:
                return query(interface)
            except Exception:
                logger.debug("UI Automation cast to %s failed", interface_name, exc_info=True)
    return pattern


def _selected_text_from_element(element):
    """Highlighted fragment exposed by one element's TextPattern, if any."""
    pattern = _pattern(element, _TEXT_PATTERN_ID, "IUIAutomationTextPattern")
    if pattern is None:
        return ""
    try:
        return _text_of_selection(pattern.GetSelection())
    except Exception:
        logger.debug("TextPattern selection read failed", exc_info=True)
        return ""


def _text_container(element):
    """Enclosing text element for a Chrome-style text run."""
    pattern = _pattern(element, _TEXT_CHILD_PATTERN_ID, "IUIAutomationTextChildPattern")
    if pattern is None:
        return None
    container = getattr(pattern, "TextContainer", None)
    if container is not None:
        return container
    accessor = getattr(pattern, "get_TextContainer", None)
    if not callable(accessor):
        return None
    try:
        return accessor()
    except Exception:
        logger.debug("Text container read failed", exc_info=True)
        return None


def _selection_along_ancestors(element, parent_of, *, limit=_MAX_ANCESTORS):
    """Read the focused control, its text container, then its parents."""
    seen = 0
    visited = set()
    while element is not None and seen <= limit and id(element) not in visited:
        visited.add(id(element))
        text = _selected_text_from_element(element)
        if text:
            return text
        container = _text_container(element)
        if container is not None and id(container) not in visited:
            text = _selected_text_from_element(container)
            if text:
                return text
        seen += 1
        try:
            element = parent_of(element)
        except Exception:
            logger.debug("UI Automation parent walk failed", exc_info=True)
            return ""
    return ""


def _uia_interface(name):
    module = _load_uia_module()
    if module is None:
        return None
    return getattr(module, name, None)


def _load_uia_module():
    global _uia_module, _uia_module_checked
    if _uia_module_checked:
        return _uia_module
    _uia_module_checked = True
    try:
        import comtypes.client

        _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
    except Exception:
        logger.debug("UI Automation type library is unavailable", exc_info=True)
        _uia_module = None
    return _uia_module


def _automation_client():
    import comtypes.client

    interface = _uia_interface("IUIAutomation")
    if interface is None:
        return comtypes.client.CreateObject(_AUTOMATION_PROGID)
    return comtypes.client.CreateObject(_AUTOMATION_PROGID, interface=interface)


def _wake_foreground_window(automation):
    """Ask the foreground window for its UI Automation provider before reading."""
    import ctypes

    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return
    try:
        automation.ElementFromHandle(hwnd)
    except Exception:
        logger.debug("UI Automation foreground element request failed", exc_info=True)


def _read_via_comtypes():
    automation = _automation_client()
    _wake_foreground_window(automation)
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


class _SelectionAccess:
    def __init__(self):
        self._automation = None
        self._handler = None

    def start(self):
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        interface = _uia_interface("IUIAutomationFocusChangedEventHandler")
        if interface is None:
            raise RuntimeError("UI Automation focus handler is unavailable")
        automation = _automation_client()

        class _Handler(comtypes.COMObject):
            _com_interfaces_ = [interface]

            def HandleFocusChangedEvent(self, sender):
                return 0

        handler = _Handler()
        automation.AddFocusChangedEventHandler(None, handler)
        self._automation = automation
        self._handler = handler

    def stop(self):
        automation = self._automation
        handler = self._handler
        self._automation = None
        self._handler = None
        if automation is None or handler is None:
            return
        try:
            automation.RemoveFocusChangedEventHandler(handler)
        except Exception:
            logger.debug("UI Automation focus listener removal failed", exc_info=True)

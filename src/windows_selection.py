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
_REPORT_TEXT_LIMIT = 180
_CONTROL_TYPES = {
    50004: "Edit",
    50020: "Text",
    50025: "Custom",
    50026: "Group",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50032: "Window",
    50033: "Pane",
    50036: "Table",
}
_CUIAutomation_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

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


def _automation_clsid():
    """CLSID of UIAutomationCore. Its ProgID is not registered on every machine."""
    module = _load_uia_module()
    coclass = getattr(module, "CUIAutomation", None) if module is not None else None
    return getattr(coclass, "_reg_clsid_", None) or _CUIAutomation_CLSID


def _automation_client():
    import comtypes

    clsid = _automation_clsid()
    if not isinstance(clsid, comtypes.GUID):
        clsid = comtypes.GUID(str(clsid))
    return comtypes.CoCreateInstance(
        clsid,
        interface=_uia_interface("IUIAutomation"),
        clsctx=comtypes.CLSCTX_INPROC_SERVER,
    )


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


def _clip(value):
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= _REPORT_TEXT_LIMIT:
        return text
    return text[:_REPORT_TEXT_LIMIT] + f"... ({len(text)} chars)"


def _attempt(func):
    try:
        return True, func()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _property(element, *names):
    for name in names:
        ok, value = _attempt(lambda name=name: getattr(element, name))
        if not ok:
            if value.startswith("AttributeError:"):
                continue
            return value
        if value is None or value == "" or callable(value):
            continue
        return str(value)
    return ""


def _element_label(element):
    name = _clip(_property(element, "CurrentName", "Name") or "(no name)")
    control = _property(element, "CurrentControlType", "ControlType")
    try:
        control_id = int(control)
    except (TypeError, ValueError):
        control_name = control or "unknown"
    else:
        control_name = f"{_CONTROL_TYPES.get(control_id, 'Control')} ({control_id})"
    framework = _property(element, "CurrentFrameworkId", "FrameworkId")
    class_name = _property(element, "CurrentClassName", "ClassName")
    details = [control_name]
    if framework:
        details.append(framework)
    if class_name:
        details.append(class_name)
    return f"{name} | {', '.join(details)}"


def _describe_pattern_call(element, pattern_id, interface_name, label):
    ok, pattern = _attempt(lambda: element.GetCurrentPattern(pattern_id))
    if not ok:
        return [f"  {label}: {pattern}"]
    if pattern is None:
        return [f"  {label}: none"]
    query = getattr(pattern, "QueryInterface", None)
    interface = _uia_interface(interface_name) if callable(query) else None
    if interface is not None:
        cast_ok, cast_pattern = _attempt(lambda: query(interface))
        if cast_ok:
            pattern = cast_pattern
        else:
            return [f"  {label}: {cast_pattern}"]
    return [f"  {label}: {type(pattern).__name__}", pattern]


def _describe_element_selection(element):
    lines = []
    described = _describe_pattern_call(
        element, _TEXT_PATTERN_ID, "IUIAutomationTextPattern", "text_pattern")
    pattern = described[-1] if not isinstance(described[-1], str) else None
    if pattern is None:
        lines.extend(described)
    else:
        lines.append(described[0])
        ok, selection = _attempt(pattern.GetSelection)
        if not ok:
            lines.append(f"  selection: {selection}")
        else:
            lines.append(f"  selection: {type(selection).__name__} {_clip(repr(selection))}")
            for index, text_range in enumerate(_iter_ranges(selection)):
                read_ok, text = _attempt(lambda text_range=text_range: _range_text(text_range))
                if read_ok:
                    lines.append(f"  range[{index}]: {_clip(text)!r}")
                else:
                    lines.append(f"  range[{index}]: {text}")
            lines.append(f"  parsed: {_text_of_selection(selection)!r}")
    child = _describe_pattern_call(
        element, _TEXT_CHILD_PATTERN_ID, "IUIAutomationTextChildPattern", "text_child")
    child_pattern = child[-1] if not isinstance(child[-1], str) else None
    if child_pattern is None:
        lines.extend(child)
        return lines
    lines.append(child[0])
    container = getattr(child_pattern, "TextContainer", None)
    if container is None:
        accessor = getattr(child_pattern, "get_TextContainer", None)
        if callable(accessor):
            ok, container = _attempt(accessor)
            if not ok:
                lines.append(f"  text_container: {container}")
                return lines
    if container is None:
        lines.append("  text_container: none")
    else:
        lines.append(f"  text_container: {_element_label(container)}")
    return lines


def _foreground_label():
    import ctypes
    import os

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    pid = ctypes.c_ulong()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    if hwnd:
        user32.GetWindowTextW(hwnd, title, len(title))
    return (
        f"foreground: hwnd={hwnd} pid={int(pid.value)} "
        f"self={os.getpid()} title={_clip(title.value)}"
    )


def _wake_result(automation):
    import ctypes

    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return "no foreground window"
    ok, result = _attempt(lambda: automation.ElementFromHandle(hwnd))
    if ok:
        return "ok"
    return result


def describe_selection_chain(element, parent_of, *, limit=_MAX_ANCESTORS):
    """Report each element's text pattern without hiding the failure."""
    lines = []
    seen = 0
    visited = set()
    origin = element
    while element is not None and seen <= limit and id(element) not in visited:
        visited.add(id(element))
        lines.append(f"[{seen}] {_element_label(element)}")
        lines.extend(_describe_element_selection(element))
        seen += 1
        try:
            element = parent_of(element)
        except Exception as exc:
            lines.append(f"parent: {type(exc).__name__}: {exc}")
            break
    try:
        reader = _selection_along_ancestors(origin, parent_of, limit=limit)
    except Exception as exc:
        reader = f"{type(exc).__name__}: {exc}"
    lines.append(f"reader: {reader!r}")
    return "\n".join(lines)


def describe_focused_selection():
    """Report the foreground selection using the same UI Automation client."""
    lines = [f"typelib: {'loaded' if _load_uia_module() else 'unavailable'}"]
    try:
        automation = _automation_client()
    except Exception as exc:
        lines.append(f"client: {type(exc).__name__}: {exc}")
        return "\n".join(lines)
    lines.append(_foreground_label())
    lines.append(f"wake: {_wake_result(automation)}")
    try:
        element = automation.GetFocusedElement()
    except Exception as exc:
        lines.append(f"focused: {type(exc).__name__}: {exc}")
        return "\n".join(lines)
    if element is None:
        lines.append("focused: none")
        lines.append("reader: ''")
        return "\n".join(lines)
    try:
        walker = automation.ControlViewWalker
        parent_of = walker.GetParentElement
    except Exception as exc:
        lines.append(f"walker: {type(exc).__name__}: {exc}")

        def parent_of(_current):
            return None
    lines.append(describe_selection_chain(element, parent_of))
    return "\n".join(lines)


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

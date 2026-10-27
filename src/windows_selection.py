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


def _null_pointer(value):
    """comtypes yields a NULL pointer when a pattern or parent does not exist."""
    if value is None:
        return True
    try:
        return not bool(value)
    except Exception:
        return False


def _null_pointer_error(exc):
    if isinstance(exc, ValueError) and "NULL COM pointer" in str(exc):
        return True
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    if code in (-2147467261, 0x80004003):
        return True
    return "NULL COM pointer" in str(exc)


def _pattern(element, pattern_id, interface_name):
    getter = getattr(element, "GetCurrentPattern", None)
    if not callable(getter) or _null_pointer(element):
        return None
    try:
        pattern = getter(pattern_id)
    except Exception as exc:
        if _null_pointer_error(exc):
            return None
        logger.debug("UI Automation pattern %s failed", pattern_id, exc_info=True)
        return None
    if pattern is None or _null_pointer(pattern):
        return None
    query = getattr(pattern, "QueryInterface", None)
    if callable(query):
        interface = _uia_interface(interface_name)
        if interface is not None:
            try:
                pattern = query(interface)
            except Exception as exc:
                if _null_pointer_error(exc):
                    return None
                logger.debug("UI Automation cast to %s failed", interface_name, exc_info=True)
    if _null_pointer(pattern):
        return None
    return pattern


def _selected_text_from_element(element):
    """Highlighted fragment from TextPattern, or from a Win32 edit control."""
    pattern = _pattern(element, _TEXT_PATTERN_ID, "IUIAutomationTextPattern")
    if pattern is not None:
        try:
            text = _text_of_selection(pattern.GetSelection())
        except Exception:
            logger.debug("TextPattern selection read failed", exc_info=True)
            text = ""
        if text:
            return text
    return _edit_selection_from_element(element)


def _is_edit_class(name):
    folded = str(name or "").casefold()
    return folded == "edit" or folded.startswith("richedit")


def _native_hwnd(element):
    for name in ("CurrentNativeWindowHandle", "NativeWindowHandle"):
        ok, value = _attempt(lambda name=name: getattr(element, name))
        if not ok or value is None or value == "" or callable(value):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _edit_selection_from_element(element):
    class_name = ""
    for name in ("CurrentClassName", "ClassName"):
        ok, value = _attempt(lambda name=name: getattr(element, name))
        if ok and value and not callable(value):
            class_name = str(value)
            break
    if not _is_edit_class(class_name):
        return ""
    hwnd = _native_hwnd(element)
    if not hwnd:
        return ""
    try:
        return _selection_from_edit_control(hwnd).strip()
    except Exception:
        logger.debug("Edit control selection read failed", exc_info=True)
        return ""


def _selection_from_edit_control(hwnd):
    """Selected slice of a classic EDIT or RichEdit, without sending keys."""
    import ctypes
    from ctypes import wintypes

    send = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )(("SendMessageW", ctypes.windll.user32))
    start = wintypes.DWORD()
    end = wintypes.DWORD()
    send(hwnd, 0x00B0, ctypes.addressof(start), ctypes.addressof(end))  # EM_GETSEL
    if int(end.value) <= int(start.value):
        return ""
    length = int(send(hwnd, 0x000E, 0, 0) or 0)  # WM_GETTEXTLENGTH
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    send(hwnd, 0x000D, length + 1, ctypes.addressof(buffer))  # WM_GETTEXT
    return buffer.value[int(start.value):int(end.value)]


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
        except Exception as exc:
            if _null_pointer_error(exc):
                return ""
            logger.debug("UI Automation parent walk failed", exc_info=True)
            return ""
        if _null_pointer(element):
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
    if _null_pointer(element):
        return [f"  {label}: none"]
    ok, pattern = _attempt(lambda: element.GetCurrentPattern(pattern_id))
    if not ok:
        if "NULL COM pointer" in pattern or "Неправильный указатель" in pattern:
            return [f"  {label}: none"]
        return [f"  {label}: {pattern}"]
    if pattern is None or _null_pointer(pattern):
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
    edit_text = _edit_selection_from_element(element)
    if edit_text:
        lines.append(f"  edit_selection: {_clip(edit_text)!r}")
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
            if _null_pointer_error(exc):
                break
            lines.append(f"parent: {type(exc).__name__}: {exc}")
            break
        if _null_pointer(element):
            break
    try:
        reader = _selection_along_ancestors(origin, parent_of, limit=limit)
    except Exception as exc:
        reader = f"{type(exc).__name__}: {exc}"
    lines.append(f"reader: {reader!r}")
    return "\n".join(lines)


def describe_focused_selection():
    """Report the foreground selection using the same UI Automation client."""
    import ctypes

    return describe_captured_window(int(ctypes.windll.user32.GetForegroundWindow() or 0))


def describe_captured_window(hwnd):
    """Report selection for the window that was in front when the mouse was released."""
    lines = [f"typelib: {'loaded' if _load_uia_module() else 'unavailable'}"]
    hwnd = int(hwnd or 0)
    class_name = _window_class(hwnd)
    lines.append(f"captured: {_window_banner(hwnd)}")
    if class_name == "SunAwtFrame":
        lines.append(
            "note: this is a Java window, usually PyCharm. Its editor does not publish "
            "a text selection. Select text in Notepad or Chrome and leave that window in front."
        )
    try:
        automation = _automation_client()
    except Exception as exc:
        lines.append(f"client: {type(exc).__name__}: {exc}")
        return "\n".join(lines)
    if not hwnd:
        lines.append("captured: no window")
        return "\n".join(lines)
    try:
        element = automation.ElementFromHandle(hwnd)
    except Exception as exc:
        lines.append(f"window: {type(exc).__name__}: {exc}")
        return "\n".join(lines)
    if element is None or _null_pointer(element):
        lines.append("window: none")
        lines.append("reader: ''")
        return "\n".join(lines)
    try:
        walker = automation.ControlViewWalker
    except Exception as exc:
        lines.append(f"walker: {type(exc).__name__}: {exc}")
        lines.append(describe_selection_chain(element, lambda _current: None))
        return "\n".join(lines)
    lines.append(describe_selection_chain(element, _safe_parent(walker)))
    hit = _first_tree_selection(element, walker)
    lines.append(f"tree_hit: {hit!r}")
    try:
        focused = automation.GetFocusedElement()
        focused_text = "" if focused is None or _null_pointer(focused) else _selection_along_ancestors(
            focused, _safe_parent(walker))
    except Exception as exc:
        focused_text = ""
        lines.append(f"focused_reader: {type(exc).__name__}: {exc}")
    else:
        lines.append(f"focused_reader: {focused_text!r}")
    edit_texts = _edit_texts_in_tree(element, walker) if class_name == "Notepad" else []
    result = _translation_text(class_name, focused_text, edit_texts)
    if result:
        lines.append(f"result: {result!r}")
    elif hit:
        lines.append("result: no selection")
        lines.append("ignored: text elsewhere in the window is not the selection")
    else:
        lines.append("result: no selection")
    return "\n".join(lines)


def _safe_parent(walker):
    def parent_of(element):
        if _null_pointer(element):
            return None
        try:
            parent = walker.GetParentElement(element)
        except Exception as exc:
            if _null_pointer_error(exc):
                return None
            raise
        if _null_pointer(parent):
            return None
        return parent

    return parent_of


def _first_tree_selection(element, walker, *, limit=250, depth_limit=8):
    state = {"seen": 0}

    def visit(current, depth):
        if current is None or _null_pointer(current) or state["seen"] >= limit or depth > depth_limit:
            return ""
        state["seen"] += 1
        text = _selected_text_from_element(current)
        if text:
            return text
        try:
            child = walker.GetFirstChildElement(current)
        except Exception as exc:
            if _null_pointer_error(exc):
                return ""
            return ""
        while child is not None and not _null_pointer(child) and state["seen"] < limit:
            found = visit(child, depth + 1)
            if found:
                return found
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break
        return ""

    return visit(element, 0)


def _window_class(hwnd):
    if not hwnd:
        return ""
    import ctypes

    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_banner(hwnd):
    import ctypes
    import os

    if not hwnd:
        return "hwnd=0"
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, len(title))
    return (
        f"hwnd={hwnd} pid={int(pid.value)} self={os.getpid()} "
        f"class={_window_class(hwnd)} title={_clip(title.value)}"
    )


def _translation_text(window_class, focused_text, edit_texts):
    """Text that may be translated.

    A bare click has no range, so every source is empty. Notepad keeps the
    fragment in its single Edit child. Text found elsewhere in a window, such
    as the Chrome address bar, is not the user's selection.
    """
    if focused_text:
        return focused_text
    if window_class == "Notepad" and len(edit_texts) == 1:
        return edit_texts[0]
    return ""


def _edit_texts_in_tree(element, walker, *, limit=250, depth_limit=8):
    found = []
    state = {"seen": 0}

    def visit(current, depth):
        if current is None or _null_pointer(current) or state["seen"] >= limit or depth > depth_limit:
            return
        state["seen"] += 1
        text = _edit_selection_from_element(current)
        if text:
            found.append(text)
        try:
            child = walker.GetFirstChildElement(current)
        except Exception:
            return
        while child is not None and not _null_pointer(child) and state["seen"] < limit:
            visit(child, depth + 1)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    visit(element, 0)
    return found


def _read_via_comtypes():
    automation = _automation_client()
    _wake_foreground_window(automation)
    element = automation.GetFocusedElement()
    if element is None or _null_pointer(element):
        return ""
    try:
        walker = automation.ControlViewWalker
    except Exception:
        logger.debug("UI Automation control walker unavailable", exc_info=True)
        return _selected_text_from_element(element)
    focused_text = _selection_along_ancestors(element, _safe_parent(walker))
    if focused_text:
        return focused_text
    import ctypes

    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if _window_class(hwnd) != "Notepad":
        return ""
    try:
        root = automation.ElementFromHandle(hwnd)
    except Exception:
        logger.debug("Notepad window element is unavailable", exc_info=True)
        return ""
    return _translation_text("Notepad", "", _edit_texts_in_tree(root, walker))


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

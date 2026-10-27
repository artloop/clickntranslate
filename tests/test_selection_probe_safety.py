"""Selection probes must not block the UI or apply an obsolete selection."""
import ctypes
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PyQt5 import QtCore, QtWidgets, sip
from PyQt5.QtTest import QTest

import mode_coordinator
import platform_support
import selection_platform
import selection_quick_translate as quick
import windows_selection


def wait_probe(controller):
    deadline = time.monotonic() + 3
    while controller._inflight is not None:
        QtWidgets.QApplication.processEvents()
        assert time.monotonic() < deadline, 'Probe did not finish'
        QTest.qWait(1)


@pytest.fixture
def controller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    owner = QtCore.QObject()
    with (mock.patch.multiple(platform_support, IS_WINDOWS=False, IS_MAC=False, IS_LINUX=True),
          mock.patch.object(quick, 'is_foreign_foreground', return_value=True),
          mock.patch.object(quick, 'foreground_token', return_value=42),
          mock.patch.object(mode_coordinator, 'active_mode', return_value=None)):
        value = quick.SelectionQuickTranslateController(owner)
        value.configure(True, 'button')
        value._stop_monitor()
        try:
            yield value
        finally:
            value.dispose()
            assert QtCore.QThreadPool.globalInstance().waitForDone(3000)
            app.processEvents()
            owner.deleteLater()


def test_windows_selection_read_does_not_synthesize_keys():
    keys = mock.Mock()
    with (mock.patch.multiple(platform_support, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False),
          mock.patch('windows_selection.read_windows_selected_text', return_value='Selected text'),
          mock.patch.object(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(keybd_event=keys)), create=True),
          mock.patch.object(platform_support, 'copy_text') as copy,
          mock.patch('pyperclip.paste', return_value='Clipboard before probing') as paste):
        text = selection_platform.read_selected_text()
    assert text == 'Selected text'
    keys.assert_not_called()
    copy.assert_not_called()
    paste.assert_not_called()


class _PatternElement:
    def __init__(self, text):
        self.text = text

    def GetCurrentPattern(self, pattern_id):
        return self if self.text is not None else None

    def GetSelection(self):
        if not self.text:
            return SimpleNamespace(Length=0, GetElement=lambda index: None)
        range_item = SimpleNamespace(GetText=lambda _limit: self.text)
        return SimpleNamespace(Length=1, GetElement=lambda index: range_item)


def test_windows_selection_reads_ancestor_when_focus_has_no_text_pattern():
    focused = _PatternElement(None)
    document = _PatternElement('Highlighted sentence')
    parents = {id(focused): document, id(document): None}

    text = windows_selection._selection_along_ancestors(
        focused, lambda element: parents[id(element)])

    assert text == 'Highlighted sentence'


def test_empty_windows_selection_leaves_clipboard_and_keys_alone():
    keys = mock.Mock()
    with (mock.patch.multiple(platform_support, IS_WINDOWS=True, IS_MAC=False, IS_LINUX=False),
          mock.patch('windows_selection.read_windows_selected_text', return_value=''),
          mock.patch.object(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(keybd_event=keys)), create=True),
          mock.patch.object(platform_support, 'copy_text') as copy,
          mock.patch('pyperclip.paste') as paste):
        assert selection_platform.read_selected_text() == ''
    keys.assert_not_called()
    copy.assert_not_called()
    paste.assert_not_called()


def test_selection_clear_hides_button_and_discards_pending_text(controller):
    results = []
    controller.translate_requested.connect(results.append)
    with mock.patch.object(quick, 'read_selected_text', side_effect=['First selection', '']):
        controller._complete_probe()
        wait_probe(controller)
        assert controller._button.isVisible()
        controller._complete_probe()
        wait_probe(controller)
    assert sip.isdeleted(controller._button) or not controller._button.isVisible()
    assert controller._pending_text == ''
    controller._on_button_clicked()
    assert results == []


@pytest.mark.parametrize('current', ['', 'Different selection', 'First selection'])
def test_button_rechecks_selection_before_translating(controller, current):
    results = []
    controller.translate_requested.connect(results.append)
    with mock.patch.object(quick, 'read_selected_text', side_effect=['First selection', current]):
        controller._complete_probe()
        wait_probe(controller)
        controller._on_button_clicked()
        wait_probe(controller)
    assert results == (['First selection'] if current == 'First selection' else [])


def test_slow_probe_keeps_gui_responsive_and_coalesces_repeated_requests(controller):
    entered, release = threading.Event(), threading.Event()
    worker_threads, ticks, results = [], [], []
    controller.configure(True, 'automatic')
    controller._stop_monitor()
    controller.translate_requested.connect(results.append)

    def read(**kwargs):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return 'Hello there'

    with mock.patch.object(quick, 'read_selected_text', side_effect=read):
        try:
            controller._complete_probe()
            assert entered.wait(1)
            QtCore.QTimer.singleShot(0, lambda: ticks.append('GUI still running'))
            QtWidgets.QApplication.processEvents()
            assert ticks == ['GUI still running']
            for _ in range(25):
                controller._complete_probe()
            release.set()
            wait_probe(controller)
        finally:
            release.set()
            wait_probe(controller)
    assert len(worker_threads) == 2
    assert all(thread != threading.get_ident() for thread in worker_threads)
    assert results == ['Hello there']


@pytest.mark.parametrize('change', ['disabled', 'disposed', 'foreground', 'mode', 'new_selection'])
def test_late_worker_result_cannot_translate_after_context_changes(controller, change):
    entered, release = threading.Event(), threading.Event()
    results = []
    controller.configure(True, 'automatic')
    controller._stop_monitor()
    controller.translate_requested.connect(results.append)

    def read(**kwargs):
        entered.set()
        assert release.wait(2)
        return 'Obsolete selection'

    with mock.patch.object(quick, 'read_selected_text', side_effect=read):
        try:
            controller._complete_probe()
            assert entered.wait(1)
            if change == 'disabled':
                controller.configure(False, 'automatic')
            elif change == 'disposed':
                controller.dispose()
                controller.dispose()
            elif change == 'foreground':
                quick.foreground_token.return_value = 99
            elif change == 'mode':
                mode_coordinator.active_mode.return_value = 'ocr'
                controller._on_mode_changed()
            else:
                controller._schedule_probe()
            release.set()
            wait_probe(controller)
        finally:
            release.set()
            wait_probe(controller)
    assert results == []
    assert sip.isdeleted(controller._button) or not controller._button.isVisible()

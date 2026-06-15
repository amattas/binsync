from types import SimpleNamespace

from binsync.ui.control_panel import ControlPanel
from binsync.ui.panel_tabs.util_panel import QUtilPanel


class FakeUtilitiesPanel:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


class FakeWorker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSignal:
    def __init__(self, callback):
        self.callback = callback
        self.emitted = False

    def emit(self):
        self.emitted = True
        self.callback()


class FakeThread:
    def __init__(self):
        self.quit_called = False
        self.wait_timeout = None
        self._running = True

    def isRunning(self):
        return self._running

    def quit(self):
        self.quit_called = True
        self._running = False

    def wait(self, timeout):
        self.wait_timeout = timeout


def test_control_panel_close_clears_registered_callbacks_and_shutdowns_utilities():
    controller = SimpleNamespace()
    utilities_panel = FakeUtilitiesPanel()
    panel = SimpleNamespace(
        controller=controller,
        _utilities_panel=utilities_panel,
        update_callback=object(),
        ctx_callback=object(),
    )
    controller.ui_callback = panel.update_callback
    controller.ctx_change_callback = panel.ctx_callback
    controller.client_init_callback = object()

    ControlPanel.closeEvent(panel, object())

    assert utilities_panel.shutdown_called is True
    assert controller.ui_callback is None
    assert controller.ctx_change_callback is None
    assert controller.client_init_callback is None


def test_util_panel_shutdown_stops_client_worker_thread():
    worker = FakeWorker()
    thread = FakeThread()
    panel = SimpleNamespace(
        client_worker=worker,
        client_thread=thread,
        stop_client_worker=FakeSignal(worker.stop),
    )

    QUtilPanel.shutdown(panel)

    assert worker.stopped is True
    assert thread.quit_called is True
    assert thread.wait_timeout == 1000

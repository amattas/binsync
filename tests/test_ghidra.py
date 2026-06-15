import io
import subprocess
from types import SimpleNamespace

import binsync.interface_overrides.ghidra as ghidra_module
from binsync.interface_overrides.ghidra import ControlPanelWindow, GhidraRemoteInterfaceWrapper


class FakeProcess:
    def __init__(self):
        self.pid = 1234
        self.stderr = io.StringIO("")
        self.terminated = False
        self.killed = False
        self.waited = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = -9


class FakeServer:
    def __init__(self):
        self.socket_path = "/tmp/fake-ghidra.sock"
        self.started = False
        self.stopped = False
        self.waited = False
        self.requires_main_thread = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait_for_shutdown(self):
        self.waited = True


def test_ghidra_ui_process_handle_is_returned(monkeypatch):
    fake_proc = FakeProcess()
    monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)
    monkeypatch.setattr("binsync.interface_overrides.ghidra.subprocess.Popen", lambda *_args, **_kwargs: fake_proc)

    proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process()

    assert proc is fake_proc


def test_ghidra_ui_process_receives_explicit_server_url_and_logs_output(monkeypatch, tmp_path):
    fake_proc = FakeProcess()
    popen_calls = []
    log_path = tmp_path / "ghidra-ui.log"
    monkeypatch.setenv("BINSYNC_GHIDRA_UI_LOG_PATH", str(log_path))
    monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr("binsync.interface_overrides.ghidra.subprocess.Popen", fake_popen)

    proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process(socket_path="/tmp/declib.sock")

    assert proc is fake_proc
    assert popen_calls[0][1]["env"]["BINSYNC_GHIDRA_SERVER_URL"] == "unix:///tmp/declib.sock"
    assert popen_calls[0][1]["env"]["BINSYNC_GHIDRA_UI_LOG_PATH"] == str(log_path)
    assert popen_calls[0][1]["stdout"].name == str(log_path)
    assert popen_calls[0][1]["stderr"] is popen_calls[0][1]["stdout"]


def test_start_ghidra_ui_uses_explicit_server_url_from_environment(monkeypatch):
    discover_calls = []
    fake_deci = object()

    class FakeDecompilerClient:
        @staticmethod
        def discover(*args, **kwargs):
            discover_calls.append((args, kwargs))
            return fake_deci

    class FakeApplication:
        @staticmethod
        def instance():
            return FakeApplication()

        def setQuitOnLastWindowClosed(self, _value):
            pass

        def exec(self):
            pass

    class FakeControlPanelWindow:
        def __init__(self, deci=None):
            self.deci = deci

        def hide(self):
            pass

        def configure(self):
            return True

        def show(self):
            pass

    monkeypatch.setenv("BINSYNC_GHIDRA_SERVER_URL", "unix:///tmp/declib.sock")
    monkeypatch.setattr("declib.api.decompiler_client.DecompilerClient", FakeDecompilerClient)
    monkeypatch.setattr(ghidra_module, "QApplication", FakeApplication)
    monkeypatch.setattr(ghidra_module, "ControlPanelWindow", FakeControlPanelWindow)

    ghidra_module.start_ghidra_ui()

    assert discover_calls == [((), {"server_url": "unix:///tmp/declib.sock"})]


def test_ghidra_wrapper_shutdown_terminates_ui_process_and_server():
    fake_proc = FakeProcess()
    fake_server = FakeServer()
    wrapper = GhidraRemoteInterfaceWrapper.__new__(GhidraRemoteInterfaceWrapper)
    wrapper.gui_process = fake_proc
    wrapper.server = fake_server

    wrapper.shutdown()

    assert fake_proc.terminated is True
    assert fake_proc.waited is True
    assert fake_server.stopped is True


def test_ghidra_wrapper_waits_for_main_thread_dispatch_server(monkeypatch):
    fake_proc = FakeProcess()
    fake_server = FakeServer()
    fake_server.requires_main_thread = True
    monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)
    monkeypatch.setattr("binsync.interface_overrides.ghidra.DecompilerServer", lambda **_kwargs: fake_server)
    monkeypatch.setattr("binsync.interface_overrides.ghidra.atexit.register", lambda _callback: None)
    monkeypatch.setattr(
        GhidraRemoteInterfaceWrapper,
        "start_gui_in_new_process",
        staticmethod(lambda socket_path=None: fake_proc),
    )

    wrapper = GhidraRemoteInterfaceWrapper()

    assert wrapper.gui_process is fake_proc
    assert fake_server.started is True
    assert fake_server.waited is True


def test_ghidra_control_panel_close_requests_remote_server_stop(monkeypatch):
    calls = []

    class FakeController:
        def stop_worker_routines(self):
            calls.append("stop_workers")

        def shutdown(self):
            calls.append("controller_shutdown")

    class FakeRemoteInterface:
        def shutdown_server(self):
            calls.append("shutdown_server")

    monkeypatch.setattr("binsync.interface_overrides.ghidra.QTimer.singleShot", lambda _delay, callback: callback())
    monkeypatch.setattr("binsync.interface_overrides.ghidra.QApplication.quit", lambda: calls.append("quit"))

    window = SimpleNamespace(controller=FakeController(), _interface=FakeRemoteInterface())

    ControlPanelWindow.closeEvent(window, object())

    assert calls == ["stop_workers", "shutdown_server", "quit"]

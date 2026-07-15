import io
import subprocess
from types import SimpleNamespace

import pytest

import binsync.interface_overrides.ghidra as ghidra_module
from binsync.interface_overrides.ghidra import ControlPanelWindow, GhidraRemoteInterfaceWrapper


class FakeProcess:
    def __init__(self, returncode=None, timeout_on_first_wait=False):
        self.pid = 1234
        self.stderr = io.StringIO("")
        self.terminated = False
        self.killed = False
        self.waited = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts = []
        self._returncode = returncode
        self._timeout_on_first_wait = timeout_on_first_wait

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self.terminate_calls += 1

    def wait(self, timeout=None):
        self.waited = True
        self.wait_timeouts.append(timeout)
        if self._timeout_on_first_wait and len(self.wait_timeouts) == 1:
            raise subprocess.TimeoutExpired("ghidra-ui", timeout)
        self._returncode = 0

    def kill(self):
        self.killed = True
        self.kill_calls += 1
        self._returncode = -9


class FakeServer:
    def __init__(self):
        self.socket_path = "/tmp/fake-ghidra.sock"
        self.started = False
        self.stopped = False
        self.stop_calls = 0
        self.waited = False
        self.requires_main_thread = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.stop_calls += 1

    def wait_for_shutdown(self):
        self.waited = True


@pytest.mark.parametrize(
    "socket_path",
    [None, "/tmp/declib.sock"],
    ids=("default-environment", "explicit-server-and-log"),
)
def test_ghidra_ui_process_launch_configuration(monkeypatch, tmp_path, socket_path):
    fake_proc = FakeProcess()
    popen_calls = []
    monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
    monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
    monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))
    expected_log_path = tmp_path / ("ghidra-ui.log" if socket_path else "binsync-ghidra-ui.log")
    if socket_path is not None:
        monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, str(expected_log_path))
    monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr("binsync.interface_overrides.ghidra.subprocess.Popen", fake_popen)

    proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process(socket_path=socket_path)

    assert proc is fake_proc
    launch_env = popen_calls[0][1]["env"]
    if socket_path is None:
        assert ghidra_module.BINSYNC_GHIDRA_SERVER_URL not in launch_env
    else:
        assert launch_env[ghidra_module.BINSYNC_GHIDRA_SERVER_URL] == f"unix://{socket_path}"
    assert launch_env[ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH] == str(expected_log_path)
    assert popen_calls[0][1]["stdout"].name == str(expected_log_path)
    assert popen_calls[0][1]["stderr"] is popen_calls[0][1]["stdout"]


@pytest.mark.parametrize(
    "server_url",
    [None, "unix:///tmp/declib.sock"],
    ids=("discover-default-server", "discover-explicit-server"),
)
def test_start_ghidra_ui_discovers_child_process_server(monkeypatch, server_url):
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

    monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
    if server_url is not None:
        monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, server_url)
    monkeypatch.setattr("declib.api.decompiler_client.DecompilerClient", FakeDecompilerClient)
    monkeypatch.setattr(ghidra_module, "QApplication", FakeApplication)
    monkeypatch.setattr(ghidra_module, "ControlPanelWindow", FakeControlPanelWindow)

    ghidra_module.start_ghidra_ui()

    expected_kwargs = {"server_url": server_url} if server_url is not None else {}
    assert discover_calls == [((), expected_kwargs)]


@pytest.mark.parametrize(
    ("returncode", "timeout_on_first_wait", "expected_terminate_calls", "expected_waits", "expected_kill_calls"),
    [
        (None, False, 1, [3], 0),
        (None, True, 1, [3, 1], 1),
        (0, False, 0, [], 0),
    ],
    ids=("normal-process", "timeout-and-kill", "already-exited-process"),
)
def test_ghidra_wrapper_shutdown_terminates_ui_process_and_server(
    returncode, timeout_on_first_wait, expected_terminate_calls, expected_waits, expected_kill_calls
):
    fake_proc = FakeProcess(returncode=returncode, timeout_on_first_wait=timeout_on_first_wait)
    fake_server = FakeServer()
    wrapper = GhidraRemoteInterfaceWrapper.__new__(GhidraRemoteInterfaceWrapper)
    wrapper.gui_process = fake_proc
    wrapper.server = fake_server

    wrapper.shutdown()
    wrapper.shutdown()

    assert fake_proc.terminate_calls == expected_terminate_calls
    assert fake_proc.wait_timeouts == expected_waits
    assert fake_proc.kill_calls == expected_kill_calls
    assert fake_server.stopped is True
    assert fake_server.stop_calls == 1


@pytest.mark.parametrize("requires_main_thread", [True, False], ids=("main-thread", "background-thread"))
def test_ghidra_wrapper_waits_for_main_thread_dispatch_server(monkeypatch, requires_main_thread):
    fake_proc = FakeProcess()
    fake_server = FakeServer()
    fake_server.requires_main_thread = requires_main_thread
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
    assert fake_server.waited is requires_main_thread


@pytest.mark.parametrize(
    ("shutdown_method", "expected_shutdown_call"),
    [("shutdown_server", "shutdown_server"), ("shutdown", "interface_shutdown")],
    ids=("modern-interface", "legacy-interface"),
)
def test_ghidra_control_panel_close_requests_remote_server_stop(
    monkeypatch, shutdown_method, expected_shutdown_call
):
    calls = []

    class FakeController:
        def stop_worker_routines(self):
            calls.append("stop_workers")

        def shutdown(self):
            calls.append("controller_shutdown")

    monkeypatch.setattr("binsync.interface_overrides.ghidra.QTimer.singleShot", lambda _delay, callback: callback())
    monkeypatch.setattr("binsync.interface_overrides.ghidra.QApplication.quit", lambda: calls.append("quit"))

    remote_interface = SimpleNamespace()
    setattr(remote_interface, shutdown_method, lambda: calls.append(expected_shutdown_call))
    window = SimpleNamespace(controller=FakeController(), _interface=remote_interface)

    ControlPanelWindow.closeEvent(window, object())

    assert calls == ["stop_workers", expected_shutdown_call, "quit"]


@pytest.mark.parametrize("successful_attempt", [2, None], ids=("retry-succeeds", "exhausted"))
def test_ghidra_ui_process_launch_retries_or_exhausts(monkeypatch, tmp_path, successful_attempt):
    popen_calls = []
    processes = []
    monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
    monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ghidra_module, "sleep", lambda _seconds: None)

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        attempt = len(popen_calls)
        proc = FakeProcess(returncode=None if attempt == successful_attempt else 1)
        processes.append(proc)
        return proc

    monkeypatch.setattr(ghidra_module.subprocess, "Popen", fake_popen)

    if successful_attempt is None:
        with pytest.raises(RuntimeError, match="Exhausted all methods"):
            GhidraRemoteInterfaceWrapper.start_gui_in_new_process()
        assert len(popen_calls) == 3
    else:
        proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process()
        assert proc is processes[successful_attempt - 1]
        assert len(popen_calls) == successful_attempt

    assert popen_calls[0][0][0][0] == ghidra_module.sys.executable
    if len(popen_calls) > 1:
        assert popen_calls[1][0][0][0] == "binsync"

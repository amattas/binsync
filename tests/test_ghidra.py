import io

from binsync.interface_overrides.ghidra import GhidraRemoteInterfaceWrapper


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
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_ghidra_ui_process_handle_is_returned(monkeypatch):
    fake_proc = FakeProcess()
    monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)
    monkeypatch.setattr("binsync.interface_overrides.ghidra.subprocess.Popen", lambda *_args, **_kwargs: fake_proc)

    proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process()

    assert proc is fake_proc


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

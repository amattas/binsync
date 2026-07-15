import io
import subprocess
import sys
from types import SimpleNamespace

import pytest

import binsync.interface_overrides.ghidra as ghidra_module
from binsync.extras.aux_server.aux_server import Server
from binsync.extras.aux_server.store import ServerStore
from binsync.interface_overrides.ghidra import ControlPanelWindow, GhidraRemoteInterfaceWrapper

from binsync.ui.aux_server_panel.aux_server_window import ClientWorker
from declib.ui.qt_objects import (
    QThread,
    QWidget,
    Signal,
    QApplication,
    Slot
)
import unittest
import threading
import time
import socket
from werkzeug.serving import make_server
from contextlib import contextmanager
from declib.artifacts import Artifact, Context


class MockContext:
    def __init__(self):
        self.addr = 0x400010
        self.func_addr = 0x400000

class MockDeci:
    def __init__(self):
        self.artifact_change_callbacks:dict[Artifact, list[function]] = {Context:[]}
        self._context = MockContext()
        
    def gui_active_context(self):
        return self._context
    
    def _update_context(self, new_values:dict[str, int]):
        self._context.addr = new_values["address"]
        self._context.func_addr = new_values["function_address"]
        for callback_fn in self.artifact_change_callbacks[Context]:
            callback_fn(self._context)
        
class MockClient:
    def __init__(self, username):
        self.master_user = username

class MockController:
    """
    A minimal implementation of a BSController that contains the information necessary for a ServerClient.
    This avoids the issue of having to create the DecompilerInterface that BSControllers typically need.
    """
    def __init__(self, username):
        self.deci = MockDeci()
        self.client = MockClient(username)
        
class ServerThreadManager():
    """
    Implementation of the server that enables shutting down the server in between tests
    """
    def __init__(self, server:Server):
        self.server = make_server(server.host, server.port, server.app)
        
    def enter(self):
        self._thread = threading.Thread(target=self.server.serve_forever)
        self._thread.start()
        
    def exit(self):
        self.server.shutdown()
        self._thread.join()

class MockUser(QWidget):
    '''
    Handles ownership of ClientWorkers and their threads, as well as related signals
    '''
    connect_signal = Signal(tuple)
    stop_signal = Signal()
    add_group = Signal(str)
    delete_group = Signal(str)
    link_project = Signal(tuple)
    unlink_project = Signal(tuple)
    list_projects = Signal()
    
    def __init__(self, controller):
        super().__init__()
        self.beliefs = {}
        self.linked_projects = {}
        
        self.worker = ClientWorker(controller)
        self.thread = QThread()
        
        self.worker.moveToThread(self.thread)
        
        self.worker.context_change.connect(self._update_beliefs)
        self.worker.projects_list.connect(self._update_linked_projects)

        self.connect_signal.connect(self.worker.connect_client)
        self.stop_signal.connect(self.worker.stop)
        self.add_group.connect(self.worker.add_group)
        self.delete_group.connect(self.worker.delete_group)
        self.link_project.connect(self.worker.link_project)
        self.unlink_project.connect(self.worker.unlink_project)
        self.list_projects.connect(self.worker.update_linked_projects)
        
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        
        self.thread.start()
    
    def shutdown(self):
        self.stop_signal.emit()

    @Slot(dict)
    def _update_beliefs(self, new_beliefs):
        self.beliefs = new_beliefs
        
    @Slot(dict)
    def _update_linked_projects(self, new_projects_list):
        self.linked_projects = new_projects_list
        

class TestAuxServer(unittest.TestCase):
    HOST = "127.0.0.1"
    PORT = 7962
        
    def setUp(self):
        self.users:list[MockUser] = []
        self.app = QApplication.instance()
        if not self.app:
            self.app = QApplication([])
        
    def tearDown(self):
        # Note: Not all clients may be present in self.clients as some tests shut down the clients early
        for user in self.users:
            user.shutdown()
        time.sleep(1) # Give time for clients to finish sending disconnects so that they can begin emitting shutdown signals
        self.app.processEvents() # Process events so that threads can receive their quit signal
        
        try:
            self.server_thread_manager.exit()
        except:
            pass
        
        try:
            self.app
        except:
            pass
        else:
            self.app.quit() # type: ignore # My linter complains that app can be None here            
    
    def test_run_server(self):
        """
        Make sure that the server can start up without issues.
        """
        server = Server(self.HOST, self.PORT)
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        time.sleep(1)
        assert server.store._user_map == {} # Validate that the initial map of user functions is empty
        assert server.store._user_count == 0 # Validate that the initial user count is 0
        
    def test_single_connection(self):
        """
        Make sure a single user can connect and disconnect with no issues
        """
            
        server = Server(self.HOST, self.PORT)
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        self.users.append(MockUser(MockController("Alice")))
        
        self.users[0].connect_signal.emit((self.HOST, self.PORT))
        time.sleep(1)
        assert server.store._user_count == 1 # Verify that the server received the connection
        self.users[0].stop_signal.emit()
        time.sleep(1)
        assert server.store._user_count == 0 # Verify that server received disconnection
    
    def test_many_connections(self):
        """
        Verify server can handle multiple connections at once
        """
        num_connections = 10
        server = Server(self.HOST, self.PORT)
        controllers:list[MockController] = []
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        # Set up contexts
        for i in range(num_connections):
            controller = MockController(f"User_{i}")
            controller.deci._update_context({
                "address":0x40000+10*i,
                "function_address":0x500000+10*i
            })
            controllers.append(controller)
            self.users.append(MockUser(controller))
        
        # Start up client threads
        for user in self.users:
            user.connect_signal.emit((self.HOST, self.PORT))
        time.sleep(2)
        # Make sure that each user's function context is present in the server's storage
        contexts_dict, _ = server.store.getUserData()
        for controller in controllers:
            user_entry = contexts_dict[controller.client.master_user]
            assert user_entry["addr"] == controller.deci._context.addr
            assert user_entry["func_addr"] == controller.deci._context.func_addr
    
    def test_context_change(self):
        """
        Verify that clients contact the server when their context changes
        """ 
        server = Server(self.HOST, self.PORT)
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        controller = MockController("Alice")
        self.users.append(MockUser(controller))
        for user in self.users:
            user.connect_signal.emit((self.HOST, self.PORT))
        time.sleep(1)
        
        contexts_dict, _ = server.store.getUserData()
        user_entry = contexts_dict[controller.client.master_user]
        assert user_entry["addr"] == controller.deci._context.addr
        assert user_entry["func_addr"] == controller.deci._context.func_addr
        
        # Update!
        controller.deci._update_context({
            "address":0x444444,
            "function_address":0x454545
        })
        time.sleep(1)
        
        contexts_dict, _ = server.store.getUserData()
        user_entry = contexts_dict[controller.client.master_user]
        assert user_entry["addr"] == controller.deci._context.addr
        assert user_entry["func_addr"] == controller.deci._context.func_addr
                
    def test_see_other_clients(self):
        num_connections = 20
        server = Server(self.HOST, self.PORT)
        
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        # Set up contexts
        controllers:list[MockController] = []
        for i in range(num_connections):
            controller = MockController(f"User_{i}")
            controller.deci._update_context({
                "address":0x40000+10*i,
                "function_address":0x500000+10*i
            })
            controllers.append(controller)
            self.users.append(MockUser(controller))
        
        for user in self.users:
            user.connect_signal.emit((self.HOST, self.PORT))
        time.sleep(2)
        
        self.app.processEvents() # required for beliefs to update in this test
        
        # Make sure beliefs have been updated to something
        assert self.users[0].beliefs != {}
        # Make sure everyone's beliefs are the same
        for i in range(len(self.users)-1):
            assert self.users[i].beliefs == self.users[i+1].beliefs
        # Make sure everyone's beliefs match up with the server
        assert self.users[0].beliefs == server.store._user_map  
    
    # Modifications to ClientWorker broke these tests so they are disabled for now.
    def test_link_unlink_projects(self):
        '''
        Test: Client creates 2 new groups, links a project to each group, then deletes one group and unlinks the project in the other group
        '''
            
        server = Server(self.HOST, self.PORT)
        binsync_url = "https://github.com/binsync/binsync.git"
        binsync_group_name = "binsync"
        
        declib_url = "https://github.com/binsync/declib.git"
        declib_group_name = "declib"
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        
        user = MockUser(MockController("Alice"))
        self.users.append(user)
        
        for user in self.users:
            user.connect_signal.emit((self.HOST, self.PORT))
        
        # Client makes new groups
        user.add_group.emit(binsync_group_name)
        user.add_group.emit(declib_group_name)
        
        # Client links projects
        user.link_project.emit((binsync_url, binsync_group_name))
        user.link_project.emit((declib_url, declib_group_name))
        
        # Validate projects list contains only our one project
        user.list_projects.emit()
        time.sleep(1) # Give time for user and server to finish up their communication
        self.app.processEvents()
        assert user.linked_projects == {
            ServerStore.DEFAULT_GROUPNAME: {},
            binsync_group_name: {
                binsync_url: None
            },
            declib_group_name: {
                declib_url: None
            }
        }

        # Client deletes a group
        user.delete_group.emit(binsync_group_name)
        
        # Client unlinks a project 
        user.unlink_project.emit((declib_url, declib_group_name))
        
        # Validate projects list is empty
        user.list_projects.emit()
        time.sleep(1) # Give time for user and server to finish up their communication
        self.app.processEvents()
        assert user.linked_projects == {
            ServerStore.DEFAULT_GROUPNAME: {},
            declib_group_name: {}
        }
    
    def test_multi_user_link_unlink_projects(self):
        '''
        User A links a project, then User B lists out linked projects.
        User C then unlinks the project and User B lists out linked projects again.
        '''
            
        server = Server(self.HOST, self.PORT)
        self.server_thread_manager = ServerThreadManager(server)
        self.server_thread_manager.enter()
        user_a = MockUser(MockController("Alice"))
        self.users.append(user_a)
        user_b = MockUser(MockController("Bob"))
        self.users.append(user_b)
        user_c = MockUser(MockController("Carol"))
        self.users.append(user_c)
        
        for user in self.users:
            user.connect_signal.emit((self.HOST, self.PORT))
        
        project_url = "https://github.com/binsync/binsync.git"
        
        # Client A links project
        user_a.link_project.emit((project_url, ServerStore.DEFAULT_GROUPNAME))
        time.sleep(0.5) # Give time for server to receive the project
        
        # Client B lists out projects
        user_b.list_projects.emit()
        time.sleep(1)
        self.app.processEvents()
        self.assertEqual(user_b.linked_projects, {
            ServerStore.DEFAULT_GROUPNAME: {
                project_url: None
            }
        })
        
        # Client C unlinks project
        user_c.unlink_project.emit((project_url, ServerStore.DEFAULT_GROUPNAME))
        time.sleep(0.5) # Give time for server to receive the unlink
        
        # Client B lists out projects
        user_b.list_projects.emit()
        time.sleep(1)
        self.app.processEvents()
        assert user_b.linked_projects == {
            ServerStore.DEFAULT_GROUPNAME: {}
        }
            
if __name__ == "__main__":
    unittest.main(argv=sys.argv)


class _FakeGhidraProcess:
    def __init__(self, returncode=None, timeout_on_first_wait=False):
        self.pid = 1234
        self.stderr = io.StringIO("")
        self.terminated = False
        self.killed = False
        self.waited = False
        self.wait_timeouts = []
        self._returncode = returncode
        self._timeout_on_first_wait = timeout_on_first_wait

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        self.wait_timeouts.append(timeout)
        if self._timeout_on_first_wait and len(self.wait_timeouts) == 1:
            raise subprocess.TimeoutExpired("ghidra-ui", timeout)
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9


class _FakeGhidraServer:
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


class _RecordingDecompilerClient:
    discover_calls = []
    decompiler = object()

    @classmethod
    def discover(cls, *args, **kwargs):
        cls.discover_calls.append((args, kwargs))
        return cls.decompiler


class _FakeApplication:
    @staticmethod
    def instance():
        return _FakeApplication()

    def setQuitOnLastWindowClosed(self, _value):
        pass

    def exec(self):
        pass


class _FakeControlPanelWindow:
    def __init__(self, deci=None):
        self.deci = deci

    def hide(self):
        pass

    def configure(self):
        return True

    def show(self):
        pass


class _RecordingController:
    def __init__(self, calls):
        self.calls = calls

    def stop_worker_routines(self):
        self.calls.append("stop_workers")


class _ModernRemoteInterface:
    def __init__(self, calls):
        self.calls = calls

    def shutdown_server(self):
        self.calls.append("shutdown_server")


class _LegacyRemoteInterface:
    def __init__(self, calls):
        self.calls = calls

    def shutdown(self):
        self.calls.append("shutdown")


@pytest.fixture
def ghidra_process_launch(monkeypatch):
    fake_process = _FakeGhidraProcess()
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_process

    monkeypatch.setattr(ghidra_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ghidra_module.subprocess, "Popen", fake_popen)
    return SimpleNamespace(process=fake_process, popen_calls=popen_calls)


@pytest.fixture
def ghidra_ui_dependencies(monkeypatch):
    _RecordingDecompilerClient.discover_calls = []
    monkeypatch.setattr("declib.api.decompiler_client.DecompilerClient", _RecordingDecompilerClient)
    monkeypatch.setattr(ghidra_module, "QApplication", _FakeApplication)
    monkeypatch.setattr(ghidra_module, "ControlPanelWindow", _FakeControlPanelWindow)
    return _RecordingDecompilerClient.discover_calls


@pytest.fixture
def ghidra_server_runtime(monkeypatch):
    fake_process = _FakeGhidraProcess()
    fake_server = _FakeGhidraServer()
    monkeypatch.setattr(ghidra_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ghidra_module, "DecompilerServer", lambda **_kwargs: fake_server)
    monkeypatch.setattr(ghidra_module.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(
        GhidraRemoteInterfaceWrapper,
        "start_gui_in_new_process",
        staticmethod(lambda socket_path=None: fake_process),
    )
    return SimpleNamespace(process=fake_process, server=fake_server)


class TestGhidraRemoteLifecycle:
    @pytest.mark.parametrize(
        "socket_path",
        [
            pytest.param("/tmp/declib.sock", id="explicit-socket"),
            pytest.param(None, id="no-socket"),
        ],
    )
    @pytest.mark.parametrize(
        "configured_log_path",
        [
            pytest.param(True, id="configured-log"),
            pytest.param(False, id="default-log"),
        ],
    )
    def test_ui_process_launch_uses_server_and_log_configuration(
        self,
        monkeypatch,
        tmp_path,
        ghidra_process_launch,
        socket_path,
        configured_log_path,
    ):
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
        if configured_log_path:
            expected_log_path = tmp_path / "configured" / "ghidra-ui.log"
            monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, str(expected_log_path))
        else:
            expected_log_path = tmp_path / "binsync-ghidra-ui.log"
            monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))

        process = GhidraRemoteInterfaceWrapper.start_gui_in_new_process(socket_path=socket_path)

        assert process is ghidra_process_launch.process
        assert len(ghidra_process_launch.popen_calls) == 1
        launch_kwargs = ghidra_process_launch.popen_calls[0][1]
        if socket_path is None:
            assert ghidra_module.BINSYNC_GHIDRA_SERVER_URL not in launch_kwargs["env"]
        else:
            assert launch_kwargs["env"][ghidra_module.BINSYNC_GHIDRA_SERVER_URL] == f"unix://{socket_path}"
        assert launch_kwargs["env"][ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH] == str(expected_log_path)
        assert launch_kwargs["stdout"].name == str(expected_log_path)
        assert launch_kwargs["stderr"] is launch_kwargs["stdout"]

    @pytest.mark.parametrize(
        "server_url, expected_discover_call",
        [
            pytest.param(
                "unix:///tmp/declib.sock",
                ((), {"server_url": "unix:///tmp/declib.sock"}),
                id="explicit-server-url",
            ),
            pytest.param(None, ((), {}), id="no-server-url"),
        ],
    )
    def test_child_ui_discovers_configured_server(
        self,
        monkeypatch,
        ghidra_ui_dependencies,
        server_url,
        expected_discover_call,
    ):
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
        if server_url is not None:
            monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, server_url)

        ghidra_module.start_ghidra_ui()

        assert ghidra_ui_dependencies == [expected_discover_call]

    @pytest.mark.parametrize(
        (
            "returncode",
            "timeout_on_first_wait",
            "expected_terminated",
            "expected_waited",
            "expected_killed",
            "expected_wait_timeouts",
        ),
        [
            pytest.param(None, False, True, True, False, [3], id="normal-exit"),
            pytest.param(None, True, True, True, True, [3, 1], id="kill-after-timeout"),
            pytest.param(0, False, False, False, False, [], id="already-exited"),
        ],
    )
    def test_wrapper_shutdown_stops_ui_process_and_server(
        self,
        returncode,
        timeout_on_first_wait,
        expected_terminated,
        expected_waited,
        expected_killed,
        expected_wait_timeouts,
    ):
        fake_process = _FakeGhidraProcess(
            returncode=returncode,
            timeout_on_first_wait=timeout_on_first_wait,
        )
        fake_server = _FakeGhidraServer()
        wrapper = GhidraRemoteInterfaceWrapper.__new__(GhidraRemoteInterfaceWrapper)
        wrapper.gui_process = fake_process
        wrapper.server = fake_server

        wrapper.shutdown()

        assert fake_process.terminated is expected_terminated
        assert fake_process.waited is expected_waited
        assert fake_process.killed is expected_killed
        assert fake_process.wait_timeouts == expected_wait_timeouts
        assert fake_server.stopped is True

    @pytest.mark.parametrize("requires_main_thread", [True, False])
    def test_wrapper_waits_for_main_thread_dispatch_server(
        self,
        ghidra_server_runtime,
        requires_main_thread,
    ):
        ghidra_server_runtime.server.requires_main_thread = requires_main_thread

        wrapper = GhidraRemoteInterfaceWrapper()

        assert wrapper.gui_process is ghidra_server_runtime.process
        assert ghidra_server_runtime.server.started is True
        assert ghidra_server_runtime.server.waited is requires_main_thread

    @pytest.mark.parametrize(
        "interface_type, interface_shutdown_call",
        [
            pytest.param(_ModernRemoteInterface, "shutdown_server", id="modern-shutdown-server"),
            pytest.param(_LegacyRemoteInterface, "shutdown", id="legacy-shutdown"),
        ],
    )
    def test_control_panel_close_stops_workers_interface_and_application(
        self,
        monkeypatch,
        interface_type,
        interface_shutdown_call,
    ):
        calls = []
        monkeypatch.setattr(ghidra_module.QTimer, "singleShot", lambda _delay, callback: callback())
        monkeypatch.setattr(ghidra_module.QApplication, "quit", lambda: calls.append("quit"))
        window = SimpleNamespace(
            controller=_RecordingController(calls),
            _interface=interface_type(calls),
        )

        ControlPanelWindow.closeEvent(window, object())

        assert calls == ["stop_workers", interface_shutdown_call, "quit"]

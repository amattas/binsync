import importlib
import sys
import types

import pytest

from binsync.extras.aux_server.aux_server import Server
from binsync.extras.aux_server.store import ServerStore

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
            


class BinjaFakeBaseInterface:
    def __init__(self, *args, **kwargs):
        self.bv = kwargs.get("bv")

    def _init_gui_components(self, *args, **kwargs):
        return True


class BinjaFakeController:
    def __init__(self, decompiler_interface=None):
        self.deci = decompiler_interface
        self.stopped = False

    def check_client(self):
        return False

    def stop_worker_routines(self):
        self.stopped = True


class BinjaFakeSidebarWidget:
    def __init__(self, name):
        self.name = name
        self.layout = None

    def setLayout(self, layout):
        self.layout = layout


class BinjaFakeLayout:
    def __init__(self):
        self.widgets = []

    def addWidget(self, widget):
        self.widgets.append(widget)


BINJA_MODULE_NAME = "binsync.interface_overrides.binja"
BINJA_PARENT_MODULE_NAME = "binsync.interface_overrides"
BINJA_FAKE_MODULE_NAMES = (
    "declib.decompilers.binja.interface",
    "binaryninjaui",
    BINJA_MODULE_NAME,
)
BINJA_MISSING_MODULE = object()


@pytest.fixture
def fake_binja_modules():
    parent_module = importlib.import_module(BINJA_PARENT_MODULE_NAME)
    original_parent_attribute = vars(parent_module).get("binja", BINJA_MISSING_MODULE)
    original_modules = {
        module_name: sys.modules.get(module_name, BINJA_MISSING_MODULE)
        for module_name in BINJA_FAKE_MODULE_NAMES
    }

    fake_binja_interface_module = types.ModuleType("declib.decompilers.binja.interface")
    fake_binja_interface_module.BinjaInterface = BinjaFakeBaseInterface
    sys.modules["declib.decompilers.binja.interface"] = fake_binja_interface_module

    fake_ui_module = types.ModuleType("binaryninjaui")
    fake_ui_module.UIAction = object
    fake_ui_module.UIActionHandler = object
    fake_ui_module.Menu = object
    fake_ui_module.SidebarWidget = BinjaFakeSidebarWidget
    fake_ui_module.SidebarWidgetType = object
    fake_ui_module.Sidebar = object
    sys.modules["binaryninjaui"] = fake_ui_module
    sys.modules.pop(BINJA_MODULE_NAME, None)

    try:
        yield
    finally:
        for module_name, original_module in original_modules.items():
            if original_module is BINJA_MISSING_MODULE:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = original_module
        if original_parent_attribute is BINJA_MISSING_MODULE:
            vars(parent_module).pop("binja", None)
        else:
            parent_module.binja = original_parent_attribute


@pytest.fixture
def binja_module(fake_binja_modules, monkeypatch):
    module = importlib.import_module(BINJA_MODULE_NAME)
    monkeypatch.setattr(module, "BSController", BinjaFakeController)
    monkeypatch.setattr(module, "ControlPanel", lambda controller: {"controller": controller})
    monkeypatch.setattr(module, "QVBoxLayout", BinjaFakeLayout)

    try:
        yield module
    finally:
        sys.modules.pop(BINJA_MODULE_NAME, None)


@pytest.fixture
def binja_interface(binja_module):
    yield binja_module.BinjaBSInterface()


class TestBinjaControllerLifecycle:
    @pytest.mark.parametrize(
        ("view_tokens", "expected_controller_count", "expected_same_controller"),
        [
            pytest.param((None,), 0, None, id="none"),
            pytest.param(("shared", "shared"), 1, True, id="repeated-same-binary-view"),
            pytest.param(("first", "second"), 2, False, id="distinct-binary-views"),
        ],
    )
    def test_controller_for_bv(
        self,
        binja_interface,
        view_tokens,
        expected_controller_count,
        expected_same_controller,
    ):
        binary_views = {
            token: object()
            for token in view_tokens
            if token is not None
        }
        views = [binary_views[token] if token is not None else None for token in view_tokens]

        controllers = [binja_interface.controller_for_bv(view) for view in views]

        assert len(binja_interface.controllers) == expected_controller_count
        for view, controller in zip(views, controllers):
            if view is None:
                assert controller is None
            else:
                assert controller.deci.bv is view
        if expected_same_controller is not None:
            assert (controllers[0] is controllers[1]) is expected_same_controller

    def test_launch_config_without_current_view_is_noop(self, binja_interface):
        class FakeContext:
            def getCurrentView(self):
                return None

        class FakeActionContext:
            context = FakeContext()

        binja_interface._launch_bs_config(FakeActionContext())

        assert binja_interface.controllers == {}

    def test_sidebar_creates_controller_for_binary_view(self, binja_module, binja_interface):
        bv = object()

        widget = binja_module.BinSyncSidebarWidget(bv, binja_interface)

        assert binja_interface.controllers[bv] is widget._controller
        assert widget._controller.deci.bv is bv
        assert widget._widget == {"controller": widget._controller}
        assert widget.layout.widgets == [widget._widget]

    def test_stop_controllers_stops_and_clears_all_controllers(self, binja_interface):
        controllers = [BinjaFakeController(), BinjaFakeController()]
        binja_interface.controllers = {
            object(): controllers[0],
            object(): controllers[1],
        }

        binja_interface.stop_controllers()

        assert all(controller.stopped for controller in controllers)
        assert binja_interface.controllers == {}


if __name__ == "__main__":
    unittest.main(argv=sys.argv)

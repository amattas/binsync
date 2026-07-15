import importlib
import sys
import types

import pytest


class MockBaseBinjaInterface:
    def __init__(self, *args, **kwargs):
        self.bv = kwargs.get("bv")
        self.artifact_watchers_started = False

    def _init_gui_components(self, *args, **kwargs):
        return True

    def start_artifact_watchers(self):
        self.artifact_watchers_started = True


class MockController:
    def __init__(self, decompiler_interface=None):
        self.deci = decompiler_interface
        self.connected = False
        self.stopped = False

    def check_client(self):
        return self.connected

    def stop_worker_routines(self):
        self.stopped = True


class MockSidebarWidget:
    def __init__(self, name):
        self.name = name
        self.layout = None

    def setLayout(self, layout):
        self.layout = layout


class MockLayout:
    def __init__(self):
        self.widgets = []

    def addWidget(self, widget):
        self.widgets.append(widget)


def import_binja_module(monkeypatch):
    fake_binja_interface_mod = types.ModuleType("declib.decompilers.binja.interface")
    fake_binja_interface_mod.BinjaInterface = MockBaseBinjaInterface
    monkeypatch.setitem(sys.modules, "declib.decompilers.binja.interface", fake_binja_interface_mod)

    fake_ui = types.ModuleType("binaryninjaui")
    fake_ui.UIAction = object
    fake_ui.UIActionHandler = object
    fake_ui.Menu = object
    fake_ui.SidebarWidget = MockSidebarWidget
    fake_ui.SidebarWidgetType = object
    fake_ui.Sidebar = object
    monkeypatch.setitem(sys.modules, "binaryninjaui", fake_ui)

    module_name = "binsync.interface_overrides.binja"
    parent_module = importlib.import_module("binsync.interface_overrides")
    monkeypatch.setattr(parent_module, "binja", None, raising=False)
    delattr(parent_module, "binja")
    monkeypatch.setitem(sys.modules, module_name, None)
    sys.modules.pop(module_name)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "BSController", MockController)
    monkeypatch.setattr(module, "ControlPanel", lambda controller: {"controller": controller})
    monkeypatch.setattr(module, "QVBoxLayout", MockLayout)
    return module


class TestBinja:
    """Tests for the Binary Ninja interface override (controller cache, config launch, sidebar, shutdown).

    Note: a plain class is used instead of unittest.TestCase because pytest parameterization
    does not work on TestCase methods and results in cleaner tests.
    """

    def test_controller_per_bv(self, monkeypatch):
        binja = import_binja_module(monkeypatch)
        plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
        plugin.controllers = {}

        bv_1 = object()
        bv_2 = object()

        controller_1 = plugin.controller_for_bv(bv_1)
        controller_1_again = plugin.controller_for_bv(bv_1)
        controller_2 = plugin.controller_for_bv(bv_2)

        assert controller_1 is controller_1_again
        assert controller_1 is not controller_2
        assert controller_1.deci.bv is bv_1
        assert controller_2.deci.bv is bv_2

    @pytest.mark.parametrize(
        "case",
        ["no-view", "configures", "already-configured"],
    )
    def test_launch_config(self, monkeypatch, case):
        binja = import_binja_module(monkeypatch)
        plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
        plugin.controllers = {}
        bv = object()
        configured_controllers = []
        constructed_dialogs = []

        class MockView:
            def getData(self):
                return bv

        class MockContext:
            def getCurrentView(self):
                return None if case == "no-view" else MockView()

        class MockActionContext:
            context = MockContext()

        class MockConfigureBSDialog:
            def __init__(self, controller):
                self.controller = controller
                constructed_dialogs.append(controller)

            def exec_(self):
                configured_controllers.append(self.controller)
                self.controller.connected = True

        monkeypatch.setattr(binja, "ConfigureBSDialog", MockConfigureBSDialog)

        if case == "already-configured":
            existing_controller = plugin.controller_for_bv(bv)
            existing_controller.connected = True

        plugin._launch_bs_config(MockActionContext())

        if case == "no-view":
            assert plugin.controllers == {}
            assert configured_controllers == []
            return

        if case == "already-configured":
            assert constructed_dialogs == []
            assert configured_controllers == []
            assert plugin.controllers == {bv: existing_controller}
            return

        controller = plugin.controllers[bv]
        assert plugin.bv is bv
        assert configured_controllers == [controller]
        assert controller.connected is True
        assert controller.deci.artifact_watchers_started is True

    @pytest.mark.parametrize(
        "has_binary_view",
        [True, False],
        ids=["valid-view", "missing-view"],
    )
    def test_sidebar_requires_bv(self, monkeypatch, has_binary_view):
        binja = import_binja_module(monkeypatch)
        plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
        plugin.controllers = {}

        bv = object() if has_binary_view else None

        if not has_binary_view:
            with pytest.raises(ValueError, match="BinaryView is required"):
                binja.BinSyncSidebarWidget(bv, plugin)

            assert plugin.controllers == {}
            return

        widget = binja.BinSyncSidebarWidget(bv, plugin)

        assert widget._controller.deci.bv is bv
        assert widget._widget == {"controller": widget._controller}
        assert widget.layout.widgets == [widget._widget]

    def test_stop_controllers(self, monkeypatch):
        binja = import_binja_module(monkeypatch)
        plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
        stop_attempts = []

        class StopController:
            def __init__(self, name, raises=False):
                self.name = name
                self.raises = raises
                self.stopped = False

            def stop_worker_routines(self):
                stop_attempts.append(self.name)
                if self.raises:
                    raise RuntimeError("failed to stop")
                self.stopped = True

        controllers = [
            StopController("first"),
            StopController("failing", raises=True),
            StopController("last"),
        ]
        plugin.controllers = {object(): controller for controller in controllers}

        plugin.stop_controllers()

        assert stop_attempts == ["first", "failing", "last"]
        assert controllers[0].stopped is True
        assert controllers[1].stopped is False
        assert controllers[2].stopped is True
        assert plugin.controllers == {}


if __name__ == "__main__":
    pytest.main(args=sys.argv)

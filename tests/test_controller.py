import tempfile
from collections import defaultdict

from declib.artifacts import Comment, Function

from binsync.controller import BSController
from binsync.core.client import Client
from binsync.core.state import State


class FakeDecompilerInterface:
    name = "fake"
    binary_hash = "fake_hash"
    default_func_prefix = "sub_"

    def __init__(self):
        self.artifact_change_callbacks = defaultdict(list)
        self.functions = {
            0x400100: Function(addr=0x400100, size=0x20, name="renamed_func")
        }
        self.comments = {}
        self.global_vars = {}
        self.enums = {}
        self.typedefs = {}
        self.structs = {}
        self.patches = {}
        self.segments = {}

    def get_func_size(self, addr):
        func = self.functions.get(addr)
        return func.size if func is not None else 0

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def shutdown(self):
        pass

    def start_artifact_watchers(self):
        pass

    def stop_artifact_watchers(self):
        pass


class FailingFunctionDict(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("function set made no change")


def test_force_push_functions_immediately_commits_to_git():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            controller = BSController(decompiler_interface=FakeDecompilerInterface(), headless=True)
            controller.client = client

            controller.force_push_functions([0x400100])

            persisted_state = client.get_state(user="user0", fetch_cache=False)
            assert persisted_state.functions[0x400100].name == "renamed_func"
        finally:
            client.shutdown()

def test_force_push_functions_persists_raw_string_comments():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            deci = FakeDecompilerInterface()
            deci.comments[0x400108] = "raw ghidra comment"
            controller = BSController(decompiler_interface=deci, headless=True)
            controller.client = client

            controller.force_push_functions([0x400100])

            persisted_state = client.get_state(user="user0", fetch_cache=False)
            assert persisted_state.comments[0x400108].comment == "raw ghidra comment"
            assert persisted_state.comments[0x400108].func_addr == 0x400100
        finally:
            client.shutdown()


def test_fill_function_continues_to_address_keyed_comments_when_function_set_noops():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            deci = FakeDecompilerInterface()
            deci.functions = FailingFunctionDict(deci.functions)
            controller = BSController(decompiler_interface=deci, headless=True)
            controller.client = client

            user_state = State("user1")
            user_state.set_function(Function(addr=0x400100, size=0x20, name="remote_name"))
            user_state.set_comment(Comment(addr=0x400108, comment="remote comment", func_addr=0x400100))

            assert controller.fill_artifact(
                0x400100, artifact_type=Function, state=user_state, user="user1"
            ) is True
            assert deci.comments[0x400108].comment == "remote comment"
        finally:
            client.shutdown()


def test_schedule_job_runs_manual_work_when_auto_commit_is_disabled():
    controller = BSController(decompiler_interface=FakeDecompilerInterface(), headless=True)
    controller._auto_commit_enabled = False
    controller.push_job_scheduler.start_worker_thread()

    try:
        result = controller.schedule_job(lambda: "manual sync ran", blocking=True)
    finally:
        controller.push_job_scheduler.stop_worker_thread()

    assert result == "manual sync ran"

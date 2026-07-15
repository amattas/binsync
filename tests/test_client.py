from collections import defaultdict
import git
import os
import pathlib
import sys
import tempfile
import threading
import toml

import pytest
import unittest

from declib.artifacts import (
    Comment,
    Enum,
    Function,
    FunctionHeader,
    GlobalVariable,
    Segment,
    StackVariable,
    Struct,
    Typedef,
)
from binsync.controller import BSController
from binsync.core.client import Client
from binsync.core.state import State


class TestClient(unittest.TestCase):
    FAKE_ADDR = 0x400080

    def test_repo_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Client("user0", tmpdir, "fake_hash", init_repo=True)
            assert os.path.isdir(os.path.join(tmpdir, ".git")) is True

    def test_dirty_master_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state
            assert state.user == "user0"
            # after first creation, state is dirty
            assert state.dirty is True

            func_header = FunctionHeader("some_name", self.FAKE_ADDR)
            state.set_function_header(func_header)
            # it should be dirty still (more edits)
            assert state.dirty is True

            # commit changes so we clean it!
            client.master_state = state
            client.commit_master_state()
            state = client.master_state
            assert state.dirty is False

            # ignore cache and grab the master state from the git repo
            state = client.get_state(user="user0", fetch_cache=False)
            assert len(state.functions) == 1
            assert state.functions[self.FAKE_ADDR].header == func_header

            # git is still running at least on windows
            client.shutdown()

    def test_commit_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state

            # create changes, and verify the state recorded a message
            fh_0 = FunctionHeader("user0_func", self.FAKE_ADDR)
            state.set_function_header(fh_0)
            assert state.last_commit_msg == f"Updated {fh_0}"
            client.master_state = state

            sv_0 = StackVariable(-0x10, "u0_var", "int", 4, self.FAKE_ADDR)
            state.set_stack_variable(sv_0)
            assert state.last_commit_msg == f"Updated {sv_0}"
            client.master_state = state

            # simulate a merge from another user
            fh_1: FunctionHeader = fh_0.copy()
            fh_1.name = "user1_func"
            # a merge is any setting to the state that does not update the 'last_change' parameter
            state.set_function_header(fh_1, from_user="user1", set_last_change=False)
            assert state.last_commit_msg == f"Merged in {fh_1} from user1"
            client.master_state = state

            # now check those changes really made it into the git repo
            client.commit_master_state()
            commits = list(client.repo.iter_commits())
            assert commits[0].message == f"Merged in {fh_1} from user1\n"
            assert commits[1].message == f"Updated {sv_0}\n"
            assert commits[2].message == f"Updated {fh_0}\n"

    def test_multi_user_branch_loading(self):
        with tempfile.TemporaryDirectory() as tmpdir:

            #
            # First User
            #

            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state
            user0_func_header = FunctionHeader("user0_func", self.FAKE_ADDR)
            state.set_function_header(user0_func_header)
            client.master_state = state
            client.commit_master_state()
            client.shutdown()

            #
            # Second User
            #

            client = Client("user1", tmpdir, "fake_hash")
            state = client.master_state
            user1_func_header = FunctionHeader("user1_func", self.FAKE_ADDR)
            state.set_function_header(user1_func_header)
            client.master_state = state
            client.commit_master_state()

            assert client.master_user == "user1"
            user0_state = client.get_state(user="user0")
            assert user0_state.functions[self.FAKE_ADDR].header == user0_func_header
            assert client.master_state.functions[self.FAKE_ADDR].header == user1_func_header

    def test_corrupted_toml_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state

            func_header = FunctionHeader("some_name", self.FAKE_ADDR)
            state.set_function_header(func_header)
            client.master_state = state
            client.commit_master_state()
            client.shutdown()

            # do some emulated file corruption making this TOML no longer valid
            with open(pathlib.Path(tmpdir) / "functions" / "00400080.toml", "r+", encoding="utf-8") as file:
                file.truncate(5)

            # force a real git commit for later loading in the client
            repo = git.Repo(tmpdir)
            repo.git.add(all=True)
            repo.index.commit("corrupt")
            
            # on the creation of the client, it will load the master_state, which will result in an
            # exception because the TOML fails to load
            self.assertRaises(toml.decoder.TomlDecodeError, lambda: Client("user0", tmpdir, "fake_hash"))
            

CONTROLLER_FUNCTION_ADDR = 0x400100
CONTROLLER_FUNCTION_SIZE = 0x20


class FakeDecompilerInterface:
    name = "fake"
    binary_hash = "fake_hash"
    default_func_prefix = "sub_"

    def __init__(self):
        self.artifact_change_callbacks = defaultdict(list)
        self.functions = {
            CONTROLLER_FUNCTION_ADDR: Function(
                addr=CONTROLLER_FUNCTION_ADDR,
                size=CONTROLLER_FUNCTION_SIZE,
                name="renamed_func",
            )
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


@pytest.fixture
def controller():
    return BSController(decompiler_interface=FakeDecompilerInterface(), headless=True)


@pytest.fixture
def client(tmp_path):
    client = Client("user0", str(tmp_path), "fake_hash", init_repo=True)
    try:
        yield client
    finally:
        client.shutdown()


@pytest.fixture
def controller_client(controller, client):
    controller.client = client
    return controller, client


class TestController:
    @pytest.mark.parametrize(
        (
            "artifact",
            "decompiler_collection",
            "force_push_method",
            "identifier",
            "state_collection",
        ),
        [
            pytest.param(
                Function(
                    addr=CONTROLLER_FUNCTION_ADDR,
                    size=CONTROLLER_FUNCTION_SIZE,
                    name="renamed_func",
                ),
                "functions",
                "force_push_functions",
                CONTROLLER_FUNCTION_ADDR,
                "functions",
                id="Function",
            ),
            pytest.param(
                GlobalVariable(addr=0x500000, name="global_value", type_="int", size=4),
                "global_vars",
                "force_push_global_vars",
                0x500000,
                "global_vars",
                id="GlobalVariable",
            ),
            pytest.param(
                Struct(name="ExampleStruct", size=4, members={}),
                "structs",
                "force_push_types",
                "ExampleStruct",
                "structs",
                id="Struct",
            ),
            pytest.param(
                Enum(name="ExampleEnum", members={"VALUE": 1}),
                "enums",
                "force_push_types",
                "ExampleEnum",
                "enums",
                id="Enum",
            ),
            pytest.param(
                Typedef(name="example_t", type_="unsigned int"),
                "typedefs",
                "force_push_types",
                "example_t",
                "typedefs",
                id="Typedef",
            ),
            pytest.param(
                Segment(
                    name=".text",
                    start_addr=0x400000,
                    end_addr=0x401000,
                    permissions="r-x",
                ),
                "segments",
                "force_push_segments",
                ".text",
                "segments",
                id="Segment",
            ),
        ],
    )
    def test_force_push_immediately_persists_artifact(
        self,
        controller_client,
        artifact,
        decompiler_collection,
        force_push_method,
        identifier,
        state_collection,
    ):
        controller, client = controller_client
        getattr(controller.deci, decompiler_collection)[identifier] = artifact

        getattr(controller, force_push_method)([identifier])

        persisted_state = client.get_state(user="user0", fetch_cache=False)
        persisted_artifact = getattr(persisted_state, state_collection)[identifier]
        assert persisted_artifact == artifact

    @pytest.mark.parametrize(
        ("comment_type", "comment_addr", "inside_function"),
        [
            pytest.param(str, CONTROLLER_FUNCTION_ADDR + 0x8, True, id="raw-string-inside"),
            pytest.param(
                str,
                CONTROLLER_FUNCTION_ADDR + CONTROLLER_FUNCTION_SIZE + 1,
                False,
                id="raw-string-outside",
            ),
            pytest.param(Comment, CONTROLLER_FUNCTION_ADDR + 0x8, True, id="Comment-inside"),
            pytest.param(
                Comment,
                CONTROLLER_FUNCTION_ADDR + CONTROLLER_FUNCTION_SIZE + 1,
                False,
                id="Comment-outside",
            ),
        ],
    )
    def test_force_push_function_collects_comments(
        self, controller_client, comment_type, comment_addr, inside_function
    ):
        controller, client = controller_client
        comment_text = "raw ghidra comment" if comment_type is str else "artifact comment"
        controller.deci.comments[comment_addr] = (
            comment_text
            if comment_type is str
            else Comment(addr=comment_addr, comment=comment_text)
        )

        controller.force_push_functions([CONTROLLER_FUNCTION_ADDR])

        persisted_state = client.get_state(user="user0", fetch_cache=False)
        if inside_function:
            persisted_comment = persisted_state.comments[comment_addr]
            assert persisted_comment.comment == comment_text
            assert persisted_comment.func_addr == CONTROLLER_FUNCTION_ADDR
        else:
            assert comment_addr not in persisted_state.comments

    def test_fill_function_continues_to_address_keyed_comments_when_function_set_noops(
        self, client
    ):
        deci = FakeDecompilerInterface()
        deci.functions = FailingFunctionDict(deci.functions)
        controller = BSController(decompiler_interface=deci, headless=True)
        controller.client = client

        user_state = State("user1")
        user_state.set_function(
            Function(
                addr=CONTROLLER_FUNCTION_ADDR,
                size=CONTROLLER_FUNCTION_SIZE,
                name="remote_name",
            )
        )
        user_state.set_comment(
            Comment(
                addr=CONTROLLER_FUNCTION_ADDR + 0x8,
                comment="remote comment",
                func_addr=CONTROLLER_FUNCTION_ADDR,
            )
        )

        assert controller.fill_artifact(
            CONTROLLER_FUNCTION_ADDR,
            artifact_type=Function,
            state=user_state,
            user="user1",
        ) is True
        assert deci.comments[CONTROLLER_FUNCTION_ADDR + 0x8].comment == "remote comment"

    @pytest.mark.parametrize(
        "blocking",
        [
            pytest.param(True, id="blocking"),
            pytest.param(False, id="nonblocking"),
        ],
    )
    def test_schedule_job_runs_manual_work_when_auto_commit_is_disabled(self, controller, blocking):
        controller._auto_commit_enabled = False
        completed = threading.Event()
        scheduler = controller.push_job_scheduler
        scheduler.MAX_WAIT_TIME = 0.1

        def manual_sync():
            completed.set()
            return "manual sync ran"

        scheduler.start_worker_thread()
        try:
            result = controller.schedule_job(manual_sync, blocking=blocking)
            if blocking:
                assert result == "manual sync ran"
            else:
                assert result is None
                assert completed.wait(timeout=2)
        finally:
            scheduler.stop_worker_thread()
            scheduler._worker.join(timeout=1)


if __name__ == "__main__":
    unittest.main(argv=sys.argv)

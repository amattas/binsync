from collections import defaultdict
import git
import os
import pathlib
import sys
import tempfile
import toml

import pytest
import unittest

from declib.artifacts import (
    Function, FunctionHeader, StackVariable, Comment, Struct
)
from binsync.controller import BSController
from binsync.core.client import Client


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


class FailingCallgraphDecompilerInterface(FakeDecompilerInterface):
    def get_callgraph(self):
        raise AttributeError("'int' object has no attribute 'function'")


class CodeXrefDecompilerInterface(FakeDecompilerInterface):
    def __init__(self):
        super().__init__()
        self.functions = {
            0x400100: Function(addr=0x400100, size=0x20, name="caller"),
            0x400200: Function(addr=0x400200, size=0x20, name="callee"),
        }

    def xrefs_to(self, artifact, decompile=False, only_code=False):
        if artifact.addr == 0x400200 and only_code:
            return [self.functions[0x400100]]
        return []


class TestProgressCallgraph:
    @pytest.mark.parametrize(
        ("decompiler_interface", "expected_nodes", "expected_edges"),
        [
            pytest.param(
                FailingCallgraphDecompilerInterface,
                {0x400100},
                set(),
                id="no-xrefs",
            ),
            pytest.param(
                CodeXrefDecompilerInterface,
                {0x400100, 0x400200},
                {(0x400100, 0x400200)},
                id="caller-to-callee-xref",
            ),
        ],
    )
    def test_progress_callgraph_fallback(
        self, decompiler_interface, expected_nodes, expected_edges
    ):
        controller = BSController(
            decompiler_interface=decompiler_interface(),
            headless=True,
        )

        graph = controller.get_progress_callgraph()

        assert {func.addr for func in graph.nodes} == expected_nodes
        assert {(caller.addr, callee.addr) for caller, callee in graph.edges} == expected_edges


if __name__ == "__main__":
    unittest.main(argv=sys.argv)

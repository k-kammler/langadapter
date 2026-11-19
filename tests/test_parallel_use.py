"""An engine's units of work side by side (run_parallel): a plain pool outside
a prepared use, and inside one, the use lends itself to the threads, whose
commands are checked as the use's own, each thread writing in a directory of
its own, and the use's other refusals stay."""
import contextvars
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter.execution import executor_for, run_parallel

SRC = Path(lb.__file__).parent


def _tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in Path(root).rglob("*") if path.is_file()}


def test_the_results_come_in_order_from_several_threads():
    threads = set()
    lock = threading.Lock()

    def square(number):
        with lock:
            threads.add(threading.get_ident())
        time.sleep(0.02)
        return number * number

    assert run_parallel(square, range(8), workers=4) == [n * n for n in range(8)]
    assert len(threads) > 1
    assert run_parallel(square, [3], workers=4) == [9]


def test_one_worker_or_one_item_runs_on_the_callers_thread():
    threads = []

    def where(number):
        threads.append(threading.get_ident())
        return number

    assert run_parallel(where, range(3), workers=1) == [0, 1, 2]
    assert run_parallel(where, [5], workers=4) == [5]
    assert set(threads) == {threading.get_ident()}


def test_the_first_error_is_raised_once_every_call_has_ended():
    finished = []

    def work(number):
        if number == 1:
            raise ValueError("one call failed")
        time.sleep(0.05)
        finished.append(number)

    with pytest.raises(ValueError, match="one call failed"):
        run_parallel(work, range(4), workers=4)
    assert sorted(finished) == [0, 2, 3]


@pytest.fixture
def prepared(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "target.py").write_bytes(b"value = 1\n")
    context = lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    yield owner
    owner.close()


def test_lent_threads_run_commands_in_a_prepared_use_each_in_its_own_dir(prepared, tmp_path):
    workers = tmp_path / "workers"
    with prepared.use() as used:
        before = _tree(used.root)

        def work(number):
            own = workers / f"worker-{number}"
            own.mkdir(parents=True)
            done = executor_for(used).run(
                [sys.executable, "-c", f"open('out.txt', 'w').write('{number}')"],
                cwd=str(own), timeout=60)
            assert done.returncode == 0
            return (own / "out.txt").read_text()

        assert run_parallel(work, range(6), workers=3) == [str(n) for n in range(6)]
        assert _tree(used.root) == before
        # A thread the use did not lend is refused, during the use as before.
        refused = []

        def unlent():
            try:
                executor_for(used)
            except OwnershipRefused as error:
                refused.append(error)

        thread = threading.Thread(target=contextvars.copy_context().run, args=(unlent,))
        thread.start()
        thread.join()
        assert len(refused) == 1 and "owning thread" in str(refused[0])


def test_a_lent_thread_is_refused_what_the_use_refuses(prepared):
    with prepared.use() as used:
        changed = replace(used, environment=lb.Environment({"ADDED": "1"}))

        def work(number):
            executor_for(changed if number == 2 else used)

        with pytest.raises(OwnershipRefused, match="changed its selected environment"):
            run_parallel(work, range(4), workers=4)


def test_only_the_c_adapter_runs_work_side_by_side_through_it():
    callers = sorted(path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                     if re.search(r"\brun_parallel\(", path.read_text(encoding="utf-8"))
                     and path.name != "execution.py")
    assert callers == ["c/_mutation.py"]

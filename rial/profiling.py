import linecache
import tracemalloc
from contextlib import contextmanager
from enum import Enum
from typing import List
from timeit import default_timer as timer


class ExecutionStep(Enum):
    INIT = "Initializing the compiler and dependencies"
    SCAN_DIR = "Scanning directory for contents"
    READ_FILE = "Reading file into memory for processing"
    PARSE_FILE = "Lexing and parsing file into an AST"
    COMPILE_MOD = "Compile the file into a module"
    COMPILE_OBJ = "Compile module into object file"
    WRITE_OBJ = "Write out the object file"
    LINK_EXE = "Link all object files together into an exe"

    def __repr__(self):
        return self.name

    def __str__(self): return self.__repr__()


class ExecutionEvent:
    file: str
    time_taken_seconds: float
    step: ExecutionStep

    def __init__(self, file: str, time_taken_seconds: float, step: ExecutionStep):
        self.file = file
        self.time_taken_seconds = time_taken_seconds
        self.step = step

    def __str__(self):
        return f"{self.step} : {self.time_taken_seconds.__round__(3)}s {self.file}"


execution_events: List[ExecutionEvent] = list()

profiling: bool = False


def set_profiling(profile: bool):
    global profiling
    profiling = profile


@contextmanager
def run_with_profiling(file: str, step: ExecutionStep):
    start = 0
    if profiling:
        start = timer()
    yield
    if profiling:
        end = timer()
        execution_events.append(ExecutionEvent(file, (end - start), step))

def display_top(snapshot, key_type='lineno', limit=10):
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))
    top_stats = snapshot.statistics(key_type)

    print("Top %s lines" % limit)
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        print("#%s: %s:%s: %.1f KiB"
              % (index, frame.filename, frame.lineno, stat.size / 1024))
        line = linecache.getline(frame.filename, frame.lineno).strip()
        if line:
            print('    %s' % line)

    other = top_stats[limit:]
    if other:
        size = sum(stat.size for stat in other)
        print("%s other: %.1f KiB" % (len(other), size / 1024))
    total = sum(stat.size for stat in top_stats)
    print("Total allocated size: %.1f KiB" % (total / 1024))
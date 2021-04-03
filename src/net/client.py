from dataclasses import dataclass
import json
import multiprocessing
import re
import socket
import struct
import threading
from typing import List, Optional, Tuple, TypeVar
import zlib

from ..candidate import CandidateResult
from ..permuter import (
    EvalError,
    EvalResult,
    Feedback,
    Finished,
    Message,
    NeedMoreWork,
    Permuter,
    Task,
    WorkDone,
)
from ..profiler import Profiler
from .core import (
    Port,
    SocketPort,
    exception_to_string,
    json_array,
    json_prop,
)


@dataclass
class ServerProps:
    min_priority: float
    num_cpus: float


def _profiler_from_json(obj: dict) -> Profiler:
    ret = Profiler()
    for key in obj:
        assert isinstance(key, str), "json properties are strings"
        stat = Profiler.StatType[key]
        time = json_prop(obj, key, float)
        ret.add_stat(stat, time)
    return ret


def _result_from_json(obj: dict, source: Optional[str]) -> EvalResult:
    if "error" in obj:
        return EvalError(exc_str=json_prop(obj, "error", str), seed=None)

    profiler = _profiler_from_json(json_prop(obj, "profiler", dict))
    return CandidateResult(
        score=json_prop(obj, "score", int),
        hash=json_prop(obj, "hash", str),
        source=source,
        profiler=profiler,
    )


def _make_script_portable(source: str) -> str:
    """Parse a shell script and get rid of the machine-specific parts that
    import.py introduces. The resulting script must be run in an environment
    that has the right binaries in its $PATH, and with a current working
    directory similar to where import.py found its target's make root."""
    lines = []
    for line in source.split("\n"):
        if re.match("cd '?/", line):
            # Skip cd's to absolute directory paths. Note that shlex quotes
            # its argument with ' if it contains spaces/single quotes.
            continue
        if re.match("'?/", line):
            quote = "'" if line[0] == "'" else ""
            ind = line.find(quote + " ")
            if ind == -1:
                ind = len(line)
            else:
                ind += len(quote)
            lastind = line.rfind("/", 0, ind)
            assert lastind != -1
            # Emit a call to "which" as the first part, to ensure the called
            # binary still sees an absolute path. qemu-irix requires this,
            # for some reason.
            line = "$(which " + quote + line[lastind + 1 : ind] + ")" + line[ind:]
        lines.append(line)
    return "\n".join(lines)


class PortablePermuter:
    def __init__(self, permuter: Permuter) -> None:
        self.fn_name = permuter.fn_name
        self.filename = permuter.source_file
        self.keep_prob = permuter.keep_prob
        self.stack_differences = permuter.scorer.stack_differences
        self.compressed_source = zlib.compress(permuter.source.encode("utf-8"))
        self.base_score = permuter.base_score
        self.base_hash = permuter.base_hash

        with open(permuter.scorer.target_o, "rb") as f:
            self.target_o_bin = f.read()

        with open(permuter.compiler.compile_cmd, "r") as f2:
            self.compile_script = _make_script_portable(f2.read())


class Connection:
    _port: SocketPort
    _permuters: List[PortablePermuter]
    _task_queue: "multiprocessing.Queue[Task]"
    _feedback_queue: "multiprocessing.Queue[Feedback]"

    def __init__(
        self,
        port: SocketPort,
        permuters: List[PortablePermuter],
        task_queue: "multiprocessing.Queue[Task]",
        feedback_queue: "multiprocessing.Queue[Feedback]",
    ) -> None:
        self._port = port
        self._permuters = permuters
        self._task_queue = task_queue
        self._feedback_queue = feedback_queue

    def _send_permuters(self) -> None:
        permuter_objs = []
        for permuter in self._permuters:
            obj = {
                "fn_name": permuter.fn_name,
                "filename": permuter.filename,
                "keep_prob": permuter.keep_prob,
                "stack_differences": permuter.stack_differences,
                "compile_script": permuter.compile_script,
            }
            permuter_objs.append(obj)
        init_obj = {
            "permuters": permuter_objs,
        }
        self._port.send_json(init_obj)

        for permuter in self._permuters:
            self._port.send(permuter.compressed_source)
            self._port.send(permuter.target_o_bin)

    def run(self) -> None:
        finish_reason: Optional[str] = None
        try:
            self._send_permuters()
            msg = self._port.receive_json()
            server_nick = json_prop(msg, "server", str)
            success = json_prop(msg, "success", bool)
            if not success:
                error = json_prop(msg, "error", str)
                finish_reason = f"failed to compile: {error}"
                return
            bases = json_array(json_prop(msg, "perm_bases", list), dict)
            if len(bases) != len(self._permuters):
                raise ValueError("perm_bases has wrong size")
            for i, base in enumerate(bases):
                base_score = json_prop(base, "base_score", int)
                base_hash = json_prop(base, "base_hash", str)
                my_base_score = self._permuters[i].base_score
                my_base_hash = self._permuters[i].base_hash
                if base_score != my_base_score:
                    raise ValueError(
                        "mismatching base score! "
                        f"({base_score} instead of {my_base_score})"
                    )
                if base_hash != my_base_hash:
                    self._feedback_queue.put(
                        (Message("note: mismatching hash"), server_nick)
                    )

            self._feedback_queue.put((NeedMoreWork(), server_nick))
            finished = False

            # Main loop: send messages from the queue on to the server, and
            # vice versa. We could decrease latency a bit by doing the two in
            # parallel, but we currently don't, instead preferring to alternate
            # between the two directions. This is done for a few reasons:
            # - it's simpler
            # - in practice, sending messages from the queue to the server will
            #   never block, since "need_work" messages make sure there is
            #   enough work in the queue, and the messages we send are small.
            # - this method ensures that we don't build up arbitrarily large
            #   queues.
            while True:
                # Read a task and send it on, unless we're just waiting for
                # things to finish.
                if not finished:
                    task = self._task_queue.get()
                    if isinstance(task, Finished):
                        self._port.send_json({"type": "finish"})
                        self._port.shutdown(socket.SHUT_WR)
                        finished = True
                    else:
                        work = {
                            "type": "work",
                            "permuter": task[0],
                            "seed": task[1],
                        }
                        self._port.send_json(work)

                # Receive a result and send it on.
                msg = self._port.receive_json()
                msg_type = json_prop(msg, "type", str)
                if msg_type == "finish":
                    break

                elif msg_type == "need_work":
                    self._feedback_queue.put((NeedMoreWork(), server_nick))

                elif msg_type == "result":
                    permuter_index = json_prop(msg, "permuter", int)
                    source: Optional[str] = None
                    if msg.get("has_source") == True:
                        # Source is sent separately, compressed, since it can be large
                        # (hundreds of kilobytes is not uncommon).
                        compressed_source = self._port.receive()
                        source = zlib.decompress(compressed_source).decode("utf-8")
                    result = _result_from_json(msg, source)
                    self._feedback_queue.put(
                        (WorkDone(permuter_index, result), server_nick)
                    )

                else:
                    raise ValueError(f"Invalid message type {msg_type}")

        except EOFError:
            finish_reason = f"disconnected"

        except Exception as e:
            errmsg = exception_to_string(e)
            finish_reason = f"error: {errmsg}"

        finally:
            self._feedback_queue.put((Finished(reason=finish_reason), None))
            self._port.shutdown()
            self._port.close()


def start_client(
    port: SocketPort,
    permuters: List[Permuter],
    task_queue: "multiprocessing.Queue[Task]",
    feedback_queue: "multiprocessing.Queue[Feedback]",
    priority: float,
) -> List[threading.Thread]:
    port.send_json(
        {
            "method": "client",
            "priority": priority,
        }
    )
    obj = port.receive_json()
    if "error" in obj:
        err = json_prop(obj, "error", str)
        print(f"Failed to connect: {err}")
        return []
    num_servers = json_prop(obj, "servers", int)
    num_cores = int(json_prop(obj, "cores", float))
    print(f"Connected! {num_servers} servers online ({num_cores} cores)")
    portable_permuters = [PortablePermuter(p) for p in permuters]

    conn = Connection(
        port,
        portable_permuters,
        task_queue,
        feedback_queue,
    )

    thread = threading.Thread(target=conn.run)
    thread.daemon = True
    thread.start()

    return [thread]

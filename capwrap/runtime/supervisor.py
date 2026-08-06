"""Running a sandbox under a pseudo-terminal, and watching what it does.

Agents are interactive TUI programs.  Capturing them through pipes gives you
line-buffered mush and takes away the terminal they expect, so each container is
launched on its own PTY: the child sees a real terminal, and the daemon holds
the master end.

That single decision buys three things at once:

* **Output.**  Raw bytes stream to the web UI's terminal, unmangled.
* **Input.**  The operator can type at an agent from the browser, and another
  container can too, if it holds WRITE_INPUT.
* **A readable snapshot.**  A `pyte` screen is fed the same bytes, so the daemon
  always knows what is *currently on screen* -- needed for the overview grid and
  for replaying state to a browser that connects late, which a raw byte log
  cannot do for a full-screen TUI.

The approach is agent-agnostic on purpose: it works for claude, for aider, for a
bare shell.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import termios
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import pyte

#: Raw output kept per container, for replay on connect.  64 KiB is enough for
#: a few screens of scrollback without letting a chatty agent eat the heap.
SCROLLBACK_BYTES = 64 * 1024

DEFAULT_COLS = 120
DEFAULT_ROWS = 32


@dataclass
class ScreenSnapshot:
    """What a terminal currently shows, in a form a browser can render.

    Carries both plain text and styled runs.  The overview tiles need the
    colours -- an agent's output is mostly diffs, test results and prompts, and
    stripping the colour out of that throws away most of what makes it readable
    at a glance.
    """

    lines: list[str]
    #: Per row, a list of {t: text, f: fg, b: bg, o: bold, r: reverse}. Defaults
    #: are omitted so the common case (unstyled text) costs one key.
    styled: list[list[dict]]
    cursor: tuple[int, int]
    cols: int
    rows: int

    def to_dict(self) -> dict:
        return {
            "lines": self.lines,
            "styled": self.styled,
            "cursor": {"x": self.cursor[0], "y": self.cursor[1]},
            "cols": self.cols,
            "rows": self.rows,
        }


@dataclass
class PtySession:
    """One sandboxed process, its terminal, and everyone watching it."""

    name: str
    argv: list[str]
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    env: dict[str, str] | None = None

    pid: int | None = None
    master_fd: int | None = None
    exit_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None

    _buffer: deque[bytes] = field(default_factory=deque, init=False)
    _buffered_bytes: int = field(default=0, init=False)
    _subscribers: list[Callable[[bytes], None]] = field(default_factory=list, init=False)
    _exit_waiters: list[asyncio.Future] = field(default_factory=list, init=False)
    _screen: pyte.Screen | None = field(default=None, init=False)
    _stream: pyte.Stream | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> int:
        """Fork the sandbox onto a new PTY and begin draining it."""
        if self.pid is not None:
            raise RuntimeError(f"{self.name} is already running")

        self._screen = pyte.Screen(self.cols, self.rows)
        self._stream = pyte.Stream(self._screen)
        self._loop = asyncio.get_running_loop()

        pid, master_fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(self.argv[0], self.argv, self.env or os.environ.copy())
            except Exception:  # noqa: BLE001
                os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        self.started_at = time.time()
        os.set_blocking(master_fd, False)
        self._set_winsize(self.cols, self.rows)
        self._loop.add_reader(master_fd, self._drain)
        return pid

    def _drain(self) -> None:
        """Read whatever the terminal has produced. Called by the event loop."""
        assert self.master_fd is not None
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            # EIO on a PTY master means the child closed the slave: it exited.
            if exc.errno in (errno.EIO, errno.EBADF):
                self._on_eof()
                return
            raise
        if not data:
            self._on_eof()
            return
        self._record(data)

    def _record(self, data: bytes) -> None:
        self._buffer.append(data)
        self._buffered_bytes += len(data)
        while self._buffered_bytes > SCROLLBACK_BYTES and len(self._buffer) > 1:
            self._buffered_bytes -= len(self._buffer.popleft())

        if self._stream is not None:
            # pyte wants text; the terminal emits bytes that may split a UTF-8
            # sequence across reads, so decode leniently rather than crashing on
            # a boundary.
            self._stream.feed(data.decode("utf-8", errors="replace"))

        for callback in list(self._subscribers):
            try:
                callback(data)
            except Exception:  # noqa: BLE001 - a dead websocket must not stop capture
                pass

    def _on_eof(self) -> None:
        if self.master_fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except Exception:  # noqa: BLE001
                pass
        self.reap()

    def reap(self) -> int | None:
        """Collect the child's exit status, if it has finished."""
        if self.pid is None or self.exit_code is not None:
            return self.exit_code
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pid, status = self.pid, 0
        if pid == 0:
            return None

        self.exit_code = (
            os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode")
            else status
        )
        self.finished_at = time.time()
        self._close_master()
        for future in self._exit_waiters:
            if not future.done():
                future.set_result(self.exit_code)
        self._exit_waiters.clear()
        return self.exit_code

    def _close_master(self) -> None:
        if self.master_fd is None:
            return
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.master_fd = None

    @property
    def running(self) -> bool:
        return self.pid is not None and self.exit_code is None

    async def wait(self) -> int:
        """Block until the container exits, returning its exit code."""
        if self.exit_code is not None:
            return self.exit_code
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        self._exit_waiters.append(future)
        # The reader may have already seen EOF without a waitpid landing.
        if (code := self.reap()) is not None:
            return code
        return await future

    # -- control ---------------------------------------------------------

    def signal(self, sig: int = signal.SIGINT) -> None:
        """Signal the whole process group, not just bwrap.

        bwrap is the group leader inside the sandbox; signalling only its pid
        would leave the agent running while its supervisor died.
        """
        if self.pid is None or self.exit_code is not None:
            return
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    async def terminate(self, grace: float = 5.0) -> int | None:
        """SIGTERM, then SIGKILL if the container ignores it."""
        if not self.running:
            return self.exit_code
        self.signal(signal.SIGTERM)
        try:
            return await asyncio.wait_for(self.wait(), timeout=grace)
        except (asyncio.TimeoutError, TimeoutError):
            self.signal(signal.SIGKILL)
            try:
                return await asyncio.wait_for(self.wait(), timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                return None

    def write(self, data: str | bytes) -> int:
        """Type at the agent."""
        if self.master_fd is None:
            raise RuntimeError(f"{self.name} has no terminal to write to")
        payload = data.encode() if isinstance(data, str) else data
        return os.write(self.master_fd, payload)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self._screen is not None:
            self._screen.resize(rows, cols)
        self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        try:
            fcntl.ioctl(
                self.master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    # -- observation -----------------------------------------------------

    def subscribe(self, callback: Callable[[bytes], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    def scrollback(self) -> bytes:
        return b"".join(self._buffer)

    def snapshot(self, tail: int | None = None) -> ScreenSnapshot:
        """What the terminal shows right now.

        Rendered from the pyte screen rather than the byte log, so a client that
        connects mid-session sees the current frame instead of having to replay
        a full-screen redraw it may have missed the start of.

        `tail` returns only the last N *interesting* rows, ending at the cursor.
        A shell sits near the bottom of its screen with blank rows below and
        often blank rows above, so the top of the buffer is the least useful
        part to show in a small tile -- the prompt and the last output are what
        you actually want.
        """
        if self._screen is None:
            return ScreenSnapshot([], [], (0, 0), self.cols, self.rows)

        screen = self._screen
        display = [screen.display[y] for y in range(self.rows)]

        start, stop = 0, self.rows
        if tail is not None and tail > 0:
            last = screen.cursor.y
            for y in range(self.rows - 1, -1, -1):
                if display[y].strip():
                    last = max(last, y)
                    break
            stop = min(self.rows, last + 1)
            start = max(0, stop - tail)

        return ScreenSnapshot(
            lines=display[start:stop],
            styled=[self._styled_row(y) for y in range(start, stop)],
            cursor=(screen.cursor.x, screen.cursor.y - start),
            cols=self.cols,
            rows=stop - start,
        )

    def _styled_row(self, y: int) -> list[dict]:
        """One screen row as run-length-coalesced styled spans."""
        assert self._screen is not None
        row = self._screen.buffer[y]
        runs: list[dict] = []
        current: dict | None = None
        key: tuple | None = None

        for x in range(self.cols):
            char = row[x]
            style = (char.fg, char.bg, char.bold, char.reverse)
            if style != key:
                current = {"t": char.data}
                if char.fg != "default":
                    current["f"] = char.fg
                if char.bg != "default":
                    current["b"] = char.bg
                if char.bold:
                    current["o"] = True
                if char.reverse:
                    current["r"] = True
                runs.append(current)
                key = style
            else:
                assert current is not None
                current["t"] += char.data

        # Drop trailing whitespace so tiles do not carry 120 columns of padding.
        while runs and not runs[-1]["t"].strip() and len(runs[-1]) == 1:
            runs.pop()
        if runs:
            runs[-1]["t"] = runs[-1]["t"].rstrip() or runs[-1]["t"]
        return runs

    def status(self) -> dict:
        return {
            "name": self.name,
            "pid": self.pid,
            "running": self.running,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cols": self.cols,
            "rows": self.rows,
        }

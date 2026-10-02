"""Interactive terminal sessions for the browser.

A session runs one command (``docker exec -it ...`` or ``ssh ...``) with a
pseudo-terminal, either on this machine or on a remote lab host over the
host's SSH connection, and shuttles bytes to and from a websocket. A
capture session shows a live packet decode the same way, without a pty.
"""

import asyncio
import fcntl
import logging
import os
import shlex
import signal
import struct
import subprocess
import termios
import threading
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

OnData = Callable[[bytes], Awaitable[None]]


class TerminalSession:
    """Common interface: start(), write(), resize(), close(), wait_closed()."""

    def __init__(self, argv: list[str], cols: int, rows: int):
        self.argv = argv
        self.cols = cols
        self.rows = rows
        self.exit_code: int | None = None
        self._queue: asyncio.Queue = asyncio.Queue()

    async def output(self):
        """Yield output chunks until the process exits."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    async def wait_closed(self) -> None:
        """Wait for clean-up started by close() to finish."""


def _take_controlling_tty():
    # Runs in the child after setsid(): make the pty (stdin) its controlling
    # terminal so window-size changes reach it as SIGWINCH. Without this,
    # `docker exec -it` never learns the browser terminal was resized.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class LocalTerminal(TerminalSession):
    """Run a command on this machine attached to a new pty."""

    def start(self, loop):
        self._loop = loop
        self._master, slave = os.openpty()
        self._set_size(self._master, self.cols, self.rows)
        env = {**os.environ, "TERM": "xterm-256color"}
        self._proc = subprocess.Popen(
            self.argv, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, env=env, close_fds=True,
            preexec_fn=_take_controlling_tty,
        )
        os.close(slave)
        os.set_blocking(self._master, False)
        loop.add_reader(self._master, self._on_readable)
        logger.info("Started local terminal: %s", shlex.join(self.argv))

    def _on_readable(self):
        try:
            data = os.read(self._master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # EIO: the other side of the pty closed
        if data:
            self._queue.put_nowait(data)
            return
        self._loop.remove_reader(self._master)
        self.exit_code = self._proc.wait()
        self._queue.put_nowait(None)

    def write(self, data):
        try:
            os.write(self._master, data)
        except OSError:
            pass

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self._set_size(self._master, cols, rows)

    @staticmethod
    def _set_size(fd, cols, rows):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        try:
            self._loop.remove_reader(self._master)
        except Exception:
            pass
        if self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        try:
            os.close(self._master)
        except OSError:
            pass


class SSHTerminal(TerminalSession):
    """Run a command on a remote lab host in an SSH channel with a pty."""

    def __init__(self, ssh_client, argv, cols, rows):
        super().__init__(argv, cols, rows)
        self._ssh = ssh_client

    def start(self, loop):
        self._loop = loop
        self._chan = self._ssh.get_transport().open_session()
        self._chan.get_pty(term="xterm-256color", width=self.cols, height=self.rows)
        self._chan.exec_command(shlex.join(self.argv))
        threading.Thread(target=self._reader, daemon=True).start()
        logger.info("Started remote terminal: %s", shlex.join(self.argv))

    def _reader(self):
        try:
            while True:
                data = self._chan.recv(65536)
                if not data:
                    break
                self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
            self.exit_code = self._chan.recv_exit_status()
        except Exception as exc:
            logger.debug("SSH terminal reader stopped: %s", exc)
        finally:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def write(self, data):
        try:
            self._chan.sendall(data)
        except Exception:
            pass

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        try:
            self._chan.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def close(self):
        try:
            self._chan.close()
        except Exception:
            pass


class CaptureSession(TerminalSession):
    """A live packet decode (``clabfleet.capture.Capture``) in a terminal tab.

    Read-only apart from Ctrl+C, which stops the capture. Closing the tab
    stops tcpdump in the container; ``wait_closed()`` waits for that.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, cols: int = 120, rows: int = 30):
        super().__init__([], cols, rows)
        self._loop = loop
        self.capture = None
        self._closing = None

    def message(self, line: str) -> None:
        """Show a status or tcpdump stderr line, dimmed (any thread)."""
        data = f"\x1b[90m{line}\x1b[0m\r\n".encode()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)

    def start(self, loop):
        threading.Thread(target=self._reader, daemon=True, name="capture-reader").start()

    def _reader(self):
        try:
            while True:
                data = self.capture.read()
                if not data:
                    break
                self._loop.call_soon_threadsafe(self._queue.put_nowait,
                                                data.replace(b"\n", b"\r\n"))
            self.exit_code = self.capture.exit_code
        finally:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def write(self, data):
        if b"\x03" in data and self.capture and not self.capture.stopped:
            self._loop.run_in_executor(None, self.capture.stop, "interrupted")

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows

    def close(self):
        if self.capture and self._closing is None:
            self._closing = self._loop.run_in_executor(None, self.capture.stop)

    async def wait_closed(self):
        if self._closing is not None:
            await asyncio.shield(self._closing)

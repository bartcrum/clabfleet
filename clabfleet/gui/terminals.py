"""Interactive terminal sessions for the browser.

A session runs one command (``docker exec -it ...`` or ``ssh ...``) with a
pseudo-terminal, either on this machine or on a remote lab host over the
host's SSH connection, and shuttles bytes to and from a websocket. A
capture session shows a live packet decode the same way, without a pty;
so does a local Logs tab (``docker logs`` needs no terminal).

Output is buffered for at most ``QUEUE_CHUNKS`` chunks per session. When
the browser does not keep up, reading from the command stops (the pty
reader is paused, reader threads block) until it does, so a client that
never reads cannot make the server buffer output without end. Input that
the command does not take yet is kept, up to ``MAX_INPUT`` bytes.
"""

import asyncio
import fcntl
import logging
import os
import queue
import shlex
import signal
import struct
import subprocess
import termios
import threading
from concurrent.futures import Executor
from typing import Callable, Optional

logger = logging.getLogger(__name__)


QUEUE_CHUNKS = 16          # output chunks buffered per session (each at most READ_SIZE)
READ_SIZE = 65536
MAX_INPUT = 1024 * 1024    # bytes of input waiting for the command, per session
MESSAGE_SLACK = 8          # capture status lines on top of QUEUE_CHUNKS before dropping
EXIT_WAIT = 2              # seconds between SIGHUP and SIGKILL when a session closes


class TerminalSession:
    """Common interface: start(), write(), resize(), close(), wait_closed().

    ``cleanup``, if given, is a blocking function run (in a worker thread)
    when the session is closed while its command is still running, e.g. to
    stop the shell inside the container that ``docker exec`` started.
    """

    def __init__(self, argv: list[str], cols: int, rows: int,
                 cleanup: Optional[Callable[[], None]] = None):
        self.argv = argv
        self.cols = cols
        self.rows = rows
        self.cleanup = cleanup
        self.exit_code: int | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self._closing: Optional[asyncio.Future] = None
        # Reader threads hold one slot per queued chunk (see _put_from_thread)
        self._slots = threading.Semaphore(QUEUE_CHUNKS)

    async def output(self):
        """Yield output chunks until the process exits."""
        while True:
            chunk = await self._queue.get()
            self._drained(chunk)
            if chunk is None:
                return
            yield chunk

    def _drained(self, chunk) -> None:
        """A chunk left the queue: there is room for more output."""
        if chunk is not None and not isinstance(chunk, _Status):
            self._slots.release()

    def _put_from_thread(self, chunk: bytes) -> bool:
        """Queue output from a reader thread, blocking it while the queue is full.

        False once the session is closed (the chunk is dropped).
        """
        while not self._slots.acquire(timeout=0.5):
            if self._closed:
                return False
        if self._closed:
            return False
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        except RuntimeError:  # the event loop is gone
            return False
        return True

    def _end_from_thread(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        except RuntimeError:
            pass

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> bool:
        """Send input; False if it was dropped because too much is pending."""
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    async def wait_closed(self) -> None:
        """Wait for clean-up started by close() to finish."""
        if self._closing is not None:
            await asyncio.shield(self._closing)

    def _run_cleanup(self) -> None:
        if not self.cleanup:
            return
        try:
            self.cleanup()
        except Exception as exc:  # noqa: BLE001 - logged; the tab is gone either way
            logger.warning("Terminal clean-up failed: %s", exc)


class _Status(bytes):
    """A status line queued without a slot (see ``CaptureSession.message``)."""


def _take_controlling_tty():
    # Runs in the child after setsid(): make the pty (stdin) its controlling
    # terminal so window-size changes reach it as SIGWINCH. Without this,
    # `docker exec -it` never learns the browser terminal was resized.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class LocalTerminal(TerminalSession):
    """Run a command on this machine attached to a new pty, or with plain
    pipes when ``tty`` is false (read-only output such as ``docker logs``)."""

    def __init__(self, argv, cols, rows, cleanup=None, tty: bool = True):
        super().__init__(argv, cols, rows, cleanup)
        self.tty = tty
        self._fd = -1
        self._proc: Optional[subprocess.Popen] = None
        self._reading = False
        self._eof = False
        self._pending = bytearray()  # input the pty has not taken yet
        self._writing = False

    async def start(self, loop):
        self._loop = loop
        if self.tty:
            master, slave = os.openpty()
            self._set_size(master, self.cols, self.rows)
            env = {**os.environ, "TERM": "xterm-256color"}
            try:
                self._proc = subprocess.Popen(
                    self.argv, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True, env=env, close_fds=True,
                    preexec_fn=_take_controlling_tty,
                )
            except Exception:
                os.close(master)
                raise
            finally:
                os.close(slave)
            self._fd = master
        else:
            self._proc = subprocess.Popen(
                self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
            self._fd = self._proc.stdout.fileno()
        os.set_blocking(self._fd, False)
        self._resume()
        logger.info("Started local terminal: %s", shlex.join(self.argv))

    # --- output, with backpressure ---

    def _pause(self) -> None:
        if self._reading:
            self._loop.remove_reader(self._fd)
            self._reading = False

    def _resume(self) -> None:
        if not self._reading and not self._eof and not self._closed:
            self._loop.add_reader(self._fd, self._on_readable)
            self._reading = True

    def _drained(self, chunk) -> None:
        if self._queue.qsize() < QUEUE_CHUNKS:
            self._resume()

    def _on_readable(self):
        try:
            data = os.read(self._fd, READ_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # EIO: the other side of the pty closed
        if data:
            if not self.tty:  # no pty to turn \n into \r\n for the terminal
                data = data.replace(b"\n", b"\r\n")
            self._queue.put_nowait(data)
            if self._queue.qsize() >= QUEUE_CHUNKS:
                self._pause()  # until the websocket has taken some
            return
        self._pause()
        self._eof = True
        self._loop.run_in_executor(None, self._wait_exit).add_done_callback(
            lambda _: self._queue.put_nowait(None))

    def _wait_exit(self) -> None:
        # The output ended, so the command is exiting; don't wait on the loop
        try:
            self.exit_code = self._proc.wait(EXIT_WAIT)
        except subprocess.TimeoutExpired:
            self._kill(signal.SIGKILL)
            self.exit_code = self._proc.wait()

    # --- input ---

    def write(self, data):
        if not self.tty or self._closed:
            return True  # nothing reads it
        if len(self._pending) + len(data) > MAX_INPUT:
            return False
        self._pending += data
        self._flush()
        return True

    def _flush(self) -> None:
        while self._pending:
            try:
                n = os.write(self._fd, self._pending)
            except (BlockingIOError, InterruptedError):
                break  # the pty is full: wait until it takes more
            except OSError:
                self._pending.clear()
                break
            del self._pending[:n]
        if self._pending and not self._writing and not self._closed:
            self._loop.add_writer(self._fd, self._flush)
            self._writing = True
        elif not self._pending and self._writing:
            self._loop.remove_writer(self._fd)
            self._writing = False

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        if self.tty and not self._closed:
            self._set_size(self._fd, cols, rows)

    @staticmethod
    def _set_size(fd, cols, rows):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # --- close ---

    def _kill(self, sig) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self):
        if self._closed or self._proc is None:
            return
        self._closed = True
        self._pause()
        if self._writing:
            self._loop.remove_writer(self._fd)
            self._writing = False
        running = self._proc.poll() is None
        if running:
            self._kill(signal.SIGHUP)
        try:
            if self.tty:
                os.close(self._fd)
            else:
                self._proc.stdout.close()
        except OSError:
            pass
        self._closing = self._loop.run_in_executor(None, self._reap, running)

    def _reap(self, was_running: bool) -> None:
        """Wait for the process (no zombies), then clean up inside the node."""
        try:
            self._proc.wait(EXIT_WAIT)
        except subprocess.TimeoutExpired:
            self._kill(signal.SIGKILL)
            self._proc.wait()
        if was_running:
            self._run_cleanup()


class SSHTerminal(TerminalSession):
    """Run a command on a remote lab host in an SSH channel with a pty.

    Every blocking paramiko call (opening the channel, sending input,
    closing) runs off the event loop. Remote Logs tabs keep their pty:
    when the channel closes, sshd hangs it up, which ends ``docker logs``.
    """

    def __init__(self, ssh_client, argv, cols, rows, cleanup=None):
        super().__init__(argv, cols, rows, cleanup)
        self._ssh = ssh_client
        self._chan = None
        self._input: queue.Queue = queue.Queue()
        self._input_size = 0
        self._input_lock = threading.Lock()

    async def start(self, loop):
        self._loop = loop
        await loop.run_in_executor(None, self._open)
        threading.Thread(target=self._reader, daemon=True, name="ssh-terminal-reader").start()
        threading.Thread(target=self._writer, daemon=True, name="ssh-terminal-writer").start()
        logger.info("Started remote terminal: %s", shlex.join(self.argv))

    def _open(self) -> None:
        chan = self._ssh.get_transport().open_session(timeout=15)
        try:
            chan.get_pty(term="xterm-256color", width=self.cols, height=self.rows)
            chan.exec_command(shlex.join(self.argv))
        except Exception:
            chan.close()
            raise
        self._chan = chan

    def _reader(self):
        try:
            while True:
                data = self._chan.recv(READ_SIZE)
                if not data or not self._put_from_thread(data):
                    break
            if not self._closed:
                self.exit_code = self._chan.recv_exit_status()
        except Exception as exc:
            logger.debug("SSH terminal reader stopped: %s", exc)
        finally:
            self._end_from_thread()

    def _writer(self):
        while True:
            item = self._input.get()
            if item is None:
                return
            try:
                if item[0] == "i":
                    self._chan.sendall(item[1])
                else:
                    self._chan.resize_pty(width=item[1], height=item[2])
            except Exception as exc:  # noqa: BLE001 - the channel is gone
                logger.debug("SSH terminal writer stopped: %s", exc)
                return
            finally:
                if item[0] == "i":
                    with self._input_lock:
                        self._input_size -= len(item[1])

    def write(self, data):
        if self._closed:
            return True
        with self._input_lock:
            if self._input_size + len(data) > MAX_INPUT:
                return False
            self._input_size += len(data)
        self._input.put(("i", data))
        return True

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        if not self._closed:
            self._input.put(("r", cols, rows))

    def close(self):
        if self._closed or self._chan is None:
            return
        self._closed = True
        self._input.put(None)
        running = not self._chan.exit_status_ready()
        self._closing = self._loop.run_in_executor(None, self._close_channel, running)

    def _close_channel(self, was_running: bool) -> None:
        try:
            self._chan.close()
        except Exception:
            pass
        if was_running:
            self._run_cleanup()


class CaptureSession(TerminalSession):
    """A live packet decode (``clabfleet.capture.Capture``) in a terminal tab.

    Read-only apart from Ctrl+C, which stops the capture. Closing the tab
    stops tcpdump in the container; ``wait_closed()`` waits for that.
    Stopping runs on ``executor`` (the GUI's capture pool).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, cols: int = 120, rows: int = 30,
                 executor: Optional[Executor] = None):
        super().__init__([], cols, rows)
        self._loop = loop
        self._executor = executor
        self.capture = None

    def message(self, line: str) -> None:
        """Show a status or tcpdump stderr line, dimmed (any thread).

        Dropped when the browser is far behind: output is never held up
        for a status line.
        """
        data = _Status(f"\x1b[90m{line}\x1b[0m\r\n".encode())
        try:
            self._loop.call_soon_threadsafe(self._offer, data)
        except RuntimeError:
            pass

    def _offer(self, data: bytes) -> None:
        if self._queue.qsize() < QUEUE_CHUNKS + MESSAGE_SLACK:
            self._queue.put_nowait(data)
        else:
            logger.debug("Capture status line dropped: the browser is not reading")

    async def start(self, loop):
        threading.Thread(target=self._reader, daemon=True, name="capture-reader").start()

    def _reader(self):
        try:
            while True:
                data = self.capture.read()
                if not data:
                    break
                if not self._put_from_thread(data.replace(b"\n", b"\r\n")):
                    break
            self.exit_code = self.capture.exit_code
        finally:
            self._end_from_thread()

    def write(self, data):
        if b"\x03" in data and self.capture and not self.capture.stopped:
            self._loop.run_in_executor(self._executor, self.capture.stop, "interrupted")
        return True

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.capture:
            self._closing = self._loop.run_in_executor(self._executor, self.capture.stop)

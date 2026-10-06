"""A small SSH server in this process, for the tests of SSHRunner: real
paramiko on both ends of a socket, commands run through ``sh -c``."""

import socket
import subprocess
import threading

import paramiko


class _Accepting(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else (
            paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=_serve, args=(channel, command.decode()), daemon=True).start()
        return True


def _serve(channel, command: str) -> None:
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pump(stream, send):
        try:
            for chunk in iter(lambda: stream.read1(65536), b""):
                send(chunk)
        except (OSError, EOFError):
            pass  # the client went away

    pumps = [threading.Thread(target=pump, args=(proc.stdout, channel.sendall), daemon=True),
             threading.Thread(target=pump, args=(proc.stderr, channel.sendall_stderr), daemon=True)]
    for t in pumps:
        t.start()
    for t in pumps:
        t.join()
    try:
        channel.send_exit_status(proc.wait())
        channel.close()
    except (OSError, EOFError):
        proc.kill()


class SSHServer:
    """``with SSHServer() as server``: listens on ``server.port`` of 127.0.0.1."""

    def __init__(self):
        self.key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.transports: list = []
        self.connections = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.key)
            self.transports.append(transport)
            try:
                transport.start_server(server=_Accepting())
            except (paramiko.SSHException, EOFError, OSError):
                pass

    def drop_connections(self) -> None:
        """Cut every connection, as a host that went away would."""
        for transport in self.transports:
            transport.close()
        self.transports.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.sock.close()
        self.drop_connections()
        return False

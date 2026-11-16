"""Discover and start one local backend for a canonical evidence database."""
from __future__ import annotations

import asyncio
import fcntl
import os
import secrets
import socket
from contextlib import contextmanager
from pathlib import Path

from reflect.backend_client import API_VERSION, BackendClient, BackendEndpoint


def backend_state_dir(db_path: Path) -> Path:
    home = Path(os.environ.get('REFLECT_HOME', Path.home() / '.reflect')).expanduser()
    path = Path(db_path).expanduser().resolve()
    default = (home / 'state' / 'reflect.db').resolve()
    return home / 'state' if path == default else path.with_name(path.name + '.backend')


def instance_token(db_path: Path) -> str:
    directory = backend_state_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'backend.token'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text().strip()
        if not token:
            raise RuntimeError('Backend authorization file is empty') from None
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    return token


class BackendLocator:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.directory = backend_state_dir(self.db_path)

    @contextmanager
    def _startup_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / 'backend-start.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _ensure_locked(self) -> BackendEndpoint:
        from reflect.report_server import ReportServerConfig, ReportServerDaemon

        with self._startup_lock():
            token = instance_token(self.db_path)
            default_dir = Path(os.environ.get('REFLECT_HOME', Path.home() / '.reflect')).expanduser() / 'state'
            port = 8765
            if self.directory != default_dir:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
            daemon = ReportServerDaemon(ReportServerConfig(port=port, db_path=self.db_path,
                                                          refresh=True, open_browser=False),
                                        state_dir=self.directory)
            status = daemon.status()
            if status.running:
                if status.db_path.resolve() != self.db_path:
                    raise RuntimeError('Running backend belongs to another database')
                url = status.url.split('/?', 1)[0]
            else:
                daemon.start()
                url = f'http://127.0.0.1:{port}'
            endpoint = BackendEndpoint(url, token, str(self.db_path))
            asyncio.run(self._await_identity(endpoint))
            return endpoint

    async def _await_identity(self, endpoint: BackendEndpoint) -> None:
        import httpx

        from reflect.backend_client import BackendError

        client = BackendClient(endpoint)
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            try:
                identity = await client.request('GET', '/api/backend')
                if identity.get('api_version') != API_VERSION or identity.get('database') != str(self.db_path):
                    raise RuntimeError('Backend identity mismatch; restart Reflect with the current installation')
                return
            except BackendError as exc:
                raise RuntimeError('The running backend is incompatible; restart Reflect with the current installation') from exc
            except httpx.TransportError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError('Backend startup timed out; inspect reflect server status') from None
                await asyncio.sleep(0.1)

    def existing(self) -> BackendEndpoint | None:
        from reflect.report_server import ReportServerConfig, ReportServerDaemon
        token_path = self.directory / 'backend.token'
        if not token_path.is_file():
            return None
        status = ReportServerDaemon(ReportServerConfig(port=8765, db_path=self.db_path), state_dir=self.directory).status()
        if not status.running or status.db_path.resolve() != self.db_path:
            return None
        return BackendEndpoint(status.url.split('/?', 1)[0], token_path.read_text().strip(), str(self.db_path))

    async def ensure(self) -> BackendEndpoint:
        return await asyncio.to_thread(self._ensure_locked)

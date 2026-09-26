"""The radar backend (app.main) running inside the desktop process on 127.0.0.1."""
import asyncio
import logging
import os
import socket
import threading

log = logging.getLogger(__name__)


def free_port(preferred: int) -> int:
    """`preferred` if it is free (a stable URL to open in a browser), otherwise any free port."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("no free TCP port")


class EmbeddedServer:
    def __init__(self, port: int, env: dict[str, str]):
        # app.config reads its settings from the environment on first import.
        os.environ.update(env)
        self.port = port
        self.server = None
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def started(self) -> bool:
        return bool(self.server and self.server.started)

    @property
    def failed(self) -> bool:
        return self.error is not None or bool(self.thread and not self.thread.is_alive() and not self.started)

    def start(self) -> None:
        import uvicorn

        from app.main import app

        # log_config=None: keep the desktop's logging (uvicorn's own handlers need a console).
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False, log_config=None
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, name="radar-backend", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self.server.serve())
        except BaseException as e:  # noqa: BLE001 - reported to the UI
            self.error = e
            log.exception("backend stopped")

    def stop(self, timeout: float = 10.0) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout)

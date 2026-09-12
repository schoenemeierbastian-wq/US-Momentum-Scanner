from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class OrderflowLaunchError(RuntimeError):
    """Fehler beim sicheren Start des getrennten Orderflow-Prozesses."""


class OrderflowProcessController:
    """Startet und beendet den Orderflow-Monitor als separaten Prozess.

    Der Controller verändert keine Scanner-Signale und kommuniziert nicht mit
    der Orderflow-Engine. Er verwaltet ausschließlich den gestarteten Prozess.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def return_code(self) -> int | None:
        if self._process is None:
            return None
        return self._process.poll()

    def _module_path(self) -> Path:
        return self.project_dir / "src" / "spike_scanner" / "orderflow_app.py"

    def _python_executable(self) -> Path:
        scripts = self.project_dir / ".venv" / "Scripts"
        pythonw = scripts / "pythonw.exe"
        python = scripts / "python.exe"
        if pythonw.exists():
            return pythonw
        if python.exists():
            return python
        return Path(sys.executable)

    def start(self) -> bool:
        """Startet den Monitor. Gibt False zurück, wenn er bereits läuft."""
        if self.is_running:
            return False

        module_path = self._module_path()
        if not module_path.exists():
            raise OrderflowLaunchError(
                f"Orderflow-Modul nicht gefunden: {module_path}"
            )

        python_executable = self._python_executable()
        if not python_executable.exists():
            raise OrderflowLaunchError(
                f"Python-Interpreter nicht gefunden: {python_executable}"
            )

        env = os.environ.copy()
        src_dir = str(self.project_dir / "src")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            src_dir if not existing_pythonpath
            else src_dir + os.pathsep + existing_pythonpath
        )

        data_dir = self.project_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "orderflow_launcher.log"

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            with log_path.open("ab") as log_file:
                self._process = subprocess.Popen(
                    [str(python_executable), "-m", "spike_scanner.orderflow_app"],
                    cwd=str(self.project_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except OSError as exc:
            self._process = None
            raise OrderflowLaunchError(
                f"Orderflow-Monitor konnte nicht gestartet werden: {exc}"
            ) from exc

        return True

    def stop(self, timeout_seconds: float = 4.0) -> bool:
        """Beendet nur den Prozess, der in dieser Scanner-Sitzung gestartet wurde."""
        if not self.is_running:
            return False

        assert self._process is not None
        try:
            self._process.terminate()
            self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=timeout_seconds)
        return True

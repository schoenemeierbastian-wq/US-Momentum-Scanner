from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_FILENAME = ".momentum_sync.json"
STATE_FILENAME = ".momentum_sync_state.json"
TRANSFER_FILENAME = "US_Momentum_Scanner_Transfer.zip"
MANIFEST_FILENAME = "manifest.json"
DATABASE_ARCHIVE_NAME = "data/scanner.db"
MODELS_ARCHIVE_PREFIX = "models/"
FORMAT_VERSION = 1


class SyncError(RuntimeError):
    """Fehler bei der sicheren Geräteübergabe."""


@dataclass(slots=True)
class SyncConfig:
    shared_dir: str
    device_id: str
    delete_after_import: bool = True


@dataclass(slots=True)
class SyncStatus:
    configured: bool
    shared_dir: str = ""
    device_id: str = ""
    transfer_exists: bool = False
    transfer_path: str = ""
    source_device: str = ""
    created_at: str = ""
    snapshot_id: str = ""
    database_size: int = 0
    database_counts: dict[str, int] | None = None
    error: str = ""



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



def _safe_device_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return value.strip("-._")[:64] or "geraet"



def default_device_id() -> str:
    return _safe_device_id(os.getenv("COMPUTERNAME") or socket.gethostname() or "geraet")



def default_shared_dir() -> Path | None:
    """Return a small dedicated OneDrive transfer folder when detectable."""
    candidates: list[Path] = []
    for name in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.getenv(name)
        if value:
            candidates.append(Path(value))
    for root in candidates:
        if root.exists():
            return root / "US_Momentum_Sync"
    return None



def _resolve_from_project(project_dir: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (project_dir / path).resolve()



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()



def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for info in archive.infolist():
        target = (destination / info.filename).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SyncError(f"Unsicherer Archivpfad: {info.filename}") from exc
    archive.extractall(destination)


class MomentumDeviceSync:
    """One-way handoff sync using a temporary OneDrive transfer package.

    Active SQLite files always remain local. A source device publishes a consistent
    SQLite snapshot and model files. The other device imports the package, creates a
    local backup, and can delete the transfer package after a successful import.
    """

    def __init__(
        self,
        project_dir: Path,
        database_path: Path,
        model_path: Path,
        *,
        app_version: str = "",
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.database_path = _resolve_from_project(self.project_dir, Path(database_path))
        self.model_path = _resolve_from_project(self.project_dir, Path(model_path))
        self.app_version = app_version
        self.config_path = self.project_dir / CONFIG_FILENAME
        self.state_path = self.project_dir / STATE_FILENAME

    def load_config(self) -> SyncConfig | None:
        if not self.config_path.exists():
            return None
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            shared = str(raw.get("shared_dir") or "").strip()
            if not shared:
                return None
            return SyncConfig(
                shared_dir=shared,
                device_id=_safe_device_id(str(raw.get("device_id") or default_device_id())),
                delete_after_import=bool(raw.get("delete_after_import", True)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save_config(self, config: SyncConfig) -> None:
        config = SyncConfig(
            shared_dir=str(Path(config.shared_dir).expanduser().resolve()),
            device_id=_safe_device_id(config.device_id),
            delete_after_import=bool(config.delete_after_import),
        )
        self.config_path.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def transfer_path(self, config: SyncConfig | None = None) -> Path:
        config = config or self.load_config()
        if config is None:
            raise SyncError("Synchronisierung ist noch nicht eingerichtet.")
        return Path(config.shared_dir).expanduser().resolve() / TRANSFER_FILENAME

    def status(self) -> SyncStatus:
        config = self.load_config()
        if config is None:
            return SyncStatus(configured=False)
        transfer = self.transfer_path(config)
        status = SyncStatus(
            configured=True,
            shared_dir=config.shared_dir,
            device_id=config.device_id,
            transfer_exists=transfer.exists(),
            transfer_path=str(transfer),
        )
        if transfer.exists():
            try:
                manifest = self.read_manifest(transfer)
                status.source_device = str(manifest.get("source_device") or "")
                status.created_at = str(manifest.get("created_at") or "")
                status.snapshot_id = str(manifest.get("snapshot_id") or "")
                database = manifest.get("database") or {}
                status.database_size = int(database.get("size") or 0)
                counts = database.get("table_counts")
                status.database_counts = counts if isinstance(counts, dict) else {}
            except Exception as exc:
                status.error = str(exc)
        return status

    def read_manifest(self, transfer_path: Path | None = None) -> dict[str, Any]:
        transfer = transfer_path or self.transfer_path()
        try:
            with zipfile.ZipFile(transfer, "r") as archive:
                raw = archive.read(MANIFEST_FILENAME)
        except FileNotFoundError as exc:
            raise SyncError("Es liegt kein Transferpaket vor.") from exc
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            raise SyncError(
                "Das Transferpaket ist unvollständig. Bitte OneDrive fertig synchronisieren lassen."
            ) from exc
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncError("Das Transfermanifest ist ungültig.") from exc
        if int(manifest.get("format_version") or 0) != FORMAT_VERSION:
            raise SyncError("Die Transferdatei hat eine nicht unterstützte Version.")
        return manifest

    def _database_table_counts(self, path: Path) -> dict[str, int]:
        counts: dict[str, int] = {}
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as con:
            tables = [
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                escaped = table.replace('"', '""')
                counts[table] = int(
                    con.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
                )
        return counts

    def _check_database(self, path: Path) -> None:
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as con:
            result = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if result.lower() != "ok":
            raise SyncError(f"SQLite-Integritätsprüfung fehlgeschlagen: {result}")

    def _backup_database(self, source: Path, destination: Path) -> None:
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            raise SyncError(f"Datenbank nicht gefunden: {source}")
        uri = f"file:{source.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as src:
            with sqlite3.connect(destination, timeout=30) as dst:
                src.backup(dst)
        self._check_database(destination)

    def _model_files(self) -> list[Path]:
        directory = self.model_path.parent
        if not directory.exists():
            return []
        return sorted(path for path in directory.glob("*.joblib") if path.is_file())

    def publish(self, *, replace_existing: bool = False) -> dict[str, Any]:
        config = self.load_config()
        if config is None:
            raise SyncError("Synchronisierung ist noch nicht eingerichtet.")
        shared_dir = Path(config.shared_dir).expanduser().resolve()
        shared_dir.mkdir(parents=True, exist_ok=True)
        transfer = shared_dir / TRANSFER_FILENAME
        if transfer.exists() and not replace_existing:
            manifest = self.read_manifest(transfer)
            source = str(manifest.get("source_device") or "anderes Gerät")
            raise SyncError(
                f"Im Transferordner liegt bereits ein Paket von {source}. "
                "Dieses muss zuerst übernommen oder gelöscht werden."
            )

        with tempfile.TemporaryDirectory(prefix="momentum-sync-") as temp_name:
            temp = Path(temp_name)
            snapshot = temp / "scanner.db"
            self._backup_database(self.database_path, snapshot)
            counts = self._database_table_counts(snapshot)

            model_entries: list[dict[str, Any]] = []
            staged_models = temp / "models"
            staged_models.mkdir(parents=True, exist_ok=True)
            for model in self._model_files():
                destination = staged_models / model.name
                shutil.copy2(model, destination)
                model_entries.append(
                    {
                        "name": model.name,
                        "size": destination.stat().st_size,
                        "sha256": _sha256(destination),
                    }
                )

            manifest: dict[str, Any] = {
                "format_version": FORMAT_VERSION,
                "snapshot_id": str(uuid.uuid4()),
                "created_at": _utc_now(),
                "source_device": config.device_id,
                "app_version": self.app_version,
                "database": {
                    "archive_name": DATABASE_ARCHIVE_NAME,
                    "size": snapshot.stat().st_size,
                    "sha256": _sha256(snapshot),
                    "table_counts": counts,
                },
                "models": model_entries,
            }
            (temp / MANIFEST_FILENAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            temporary_transfer = shared_dir / f".{TRANSFER_FILENAME}.{uuid.uuid4().hex}.tmp"
            try:
                with zipfile.ZipFile(temporary_transfer, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(temp / MANIFEST_FILENAME, MANIFEST_FILENAME)
                    archive.write(snapshot, DATABASE_ARCHIVE_NAME)
                    for model in staged_models.glob("*.joblib"):
                        archive.write(model, MODELS_ARCHIVE_PREFIX + model.name)
                # Verify the complete archive before making it visible under the final name.
                self.read_manifest(temporary_transfer)
                os.replace(temporary_transfer, transfer)
            finally:
                temporary_transfer.unlink(missing_ok=True)

        self._write_state(
            {
                "last_action": "published",
                "snapshot_id": manifest["snapshot_id"],
                "at": _utc_now(),
                "source_device": config.device_id,
            }
        )
        return manifest

    def import_transfer(self, *, delete_after: bool | None = None) -> dict[str, Any]:
        config = self.load_config()
        if config is None:
            raise SyncError("Synchronisierung ist noch nicht eingerichtet.")
        transfer = self.transfer_path(config)
        manifest = self.read_manifest(transfer)
        source_device = str(manifest.get("source_device") or "")
        if source_device == config.device_id:
            raise SyncError("Dieses Transferpaket wurde auf diesem Gerät erstellt.")

        with tempfile.TemporaryDirectory(prefix="momentum-import-") as temp_name:
            temp = Path(temp_name)
            try:
                with zipfile.ZipFile(transfer, "r") as archive:
                    _safe_extract(archive, temp)
            except (zipfile.BadZipFile, OSError) as exc:
                raise SyncError(
                    "Das Transferpaket ist unvollständig. Bitte OneDrive fertig synchronisieren lassen."
                ) from exc

            database_info = manifest.get("database") or {}
            archive_name = str(database_info.get("archive_name") or DATABASE_ARCHIVE_NAME)
            incoming_db = temp / archive_name
            if not incoming_db.exists():
                raise SyncError("Die Datenbank fehlt im Transferpaket.")
            if _sha256(incoming_db) != str(database_info.get("sha256") or ""):
                raise SyncError("Die Prüfsumme der übertragenen Datenbank stimmt nicht.")
            self._check_database(incoming_db)

            for model_info in manifest.get("models") or []:
                name = Path(str(model_info.get("name") or "")).name
                incoming_model = temp / MODELS_ARCHIVE_PREFIX / name
                if not incoming_model.exists():
                    raise SyncError(f"Modelldatei fehlt im Transferpaket: {name}")
                if _sha256(incoming_model) != str(model_info.get("sha256") or ""):
                    raise SyncError(f"Prüfsumme der Modelldatei stimmt nicht: {name}")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = self.project_dir / "backups" / f"before_sync_{stamp}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            if self.database_path.exists():
                self._backup_database(self.database_path, backup_dir / self.database_path.name)
            existing_models = self._model_files()
            if existing_models:
                models_backup = backup_dir / "models"
                models_backup.mkdir(parents=True, exist_ok=True)
                for model in existing_models:
                    shutil.copy2(model, models_backup / model.name)

            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            incoming_target = self.database_path.with_name(self.database_path.name + ".incoming")
            shutil.copy2(incoming_db, incoming_target)
            for suffix in ("-wal", "-shm"):
                Path(str(self.database_path) + suffix).unlink(missing_ok=True)
            os.replace(incoming_target, self.database_path)

            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            for model in self._model_files():
                model.unlink(missing_ok=True)
            for model_info in manifest.get("models") or []:
                name = Path(str(model_info.get("name") or "")).name
                shutil.copy2(temp / MODELS_ARCHIVE_PREFIX / name, self.model_path.parent / name)

        self._write_state(
            {
                "last_action": "imported",
                "snapshot_id": str(manifest.get("snapshot_id") or ""),
                "at": _utc_now(),
                "source_device": source_device,
            }
        )

        should_delete = config.delete_after_import if delete_after is None else bool(delete_after)
        deleted = False
        if should_delete:
            try:
                transfer.unlink()
                deleted = True
            except OSError as exc:
                raise SyncError(
                    "Die Daten wurden erfolgreich übernommen, aber die Transferdatei konnte "
                    f"nicht gelöscht werden: {exc}"
                ) from exc
        result = dict(manifest)
        result["transfer_deleted"] = deleted
        result["local_backup_dir"] = str(backup_dir)
        return result

    def delete_transfer(self) -> bool:
        transfer = self.transfer_path()
        if not transfer.exists():
            return False
        transfer.unlink()
        return True

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

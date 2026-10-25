"""Managed media paths and recoverable human filename changes.

Private metadata stays in DATA_DIR. A configured media root must already carry
the exact marker; a missing disk must never silently fall back to private SSD.
"""
import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata

MARKER_CONTENT = b"playnow-storage-v1\n"
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class StorageError(Exception):
    pass


class StorageConflict(StorageError):
    pass


def validate_filename(filename, previous=None):
    if not isinstance(filename, str) or not filename or filename != filename.strip() or filename.startswith(".") or filename in (".", ".."):
        raise StorageError("Název souboru nesmí být prázdný, skrytý ani začínat nebo končit mezerou.")
    if "/" in filename or "\\" in filename or any(unicodedata.category(char) == "Cc" for char in filename):
        raise StorageError("Název souboru nesmí obsahovat cesty ani řídicí znaky.")
    try:
        if len(filename.encode("utf-8")) > 240:
            raise StorageError("Celý název souboru může mít nejvýše 240 bajtů v UTF-8.")
    except UnicodeEncodeError:
        raise StorageError("Název souboru není platný UTF-8 text.") from None
    if previous is not None and Path(filename).suffix.casefold() != Path(previous).suffix.casefold():
        raise StorageError("Příponu souboru zachovejte; změnit lze jeho název.")
    return filename


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_storage(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(videos)")}
        if "rename_to" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN rename_to TEXT")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


class MediaStorage:
    def __init__(self, data_dir, storage_dir=None):
        self.data_dir = Path(data_dir).resolve()
        self.configured = bool(storage_dir)
        if self.configured:
            requested = Path(storage_dir)
            if not requested.is_absolute() or requested.is_symlink() or not requested.is_dir():
                raise StorageError("Vyhrazený disk videí není připojený.")
            self.root = requested.resolve()
            marker = self.root / ".playnow-storage"
            if marker.is_symlink() or not marker.is_file():
                raise StorageError("Vyhrazený disk videí nemá ověřovací značku.")
            with marker.open("rb") as source:
                if source.read(64) != MARKER_CONTENT:
                    raise StorageError("Ověřovací značka disku videí není platná.")
            self.originals_dir = self.root / "Originaly"
            self.uploads_dir = self.root / "Nahravani"
            self.compatible_dir = self.root / "Prehravani"
        else:
            self.root = self.data_dir
            self.originals_dir = self.data_dir / "media"
            self.uploads_dir = self.data_dir / "uploads"
            self.compatible_dir = self.data_dir / "media"
        for path in (self.originals_dir, self.uploads_dir, self.compatible_dir):
            self._directory(path, create=True)

    def _identifier(self, identifier):
        if not ID_PATTERN.fullmatch(identifier):
            raise StorageError("Neplatný identifikátor videa.")

    def _directory(self, path, create=False):
        if path.is_symlink():
            raise StorageError("Adresář videí nesmí být symbolický odkaz.")
        if create:
            try:
                path.mkdir(mode=0o700, parents=False)
            except FileExistsError:
                pass
            else:
                # Persist a new video directory entry before staging can be removed.
                fsync_directory(path.parent)
        if path.is_symlink() or not path.is_dir():
            raise StorageError("Adresář videí není dostupný.")
        return path

    def _regular(self, path):
        try:
            snapshot = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(snapshot.st_mode):
            raise StorageConflict("Soubor videa koliduje s jiným typem položky.")
        return snapshot

    def _same_file(self, first, second):
        a, b = self._regular(first), self._regular(second)
        return bool(a and b and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))

    @contextlib.contextmanager
    def file_lock(self, identifier):
        self._identifier(identifier)
        with (self.data_dir / "locks" / identifier).open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def video_directory(self, identifier, create=False):
        self._identifier(identifier)
        return self._directory(self.originals_dir / identifier, create=create)

    def original(self, identifier):
        self._identifier(identifier)
        if self.configured:
            folder = self.originals_dir / identifier
            if folder.exists() or folder.is_symlink():
                self._directory(folder)
            path = folder / ".source"
        else:
            path = self.originals_dir / f"{identifier}.video"
        self._regular(path)
        return path

    def named_original(self, identifier, filename):
        validate_filename(filename)
        return self.video_directory(identifier) / filename if self.configured else self.original(identifier)

    def part(self, identifier, imported=False):
        self._identifier(identifier)
        path = self.uploads_dir / f"{identifier}{'.import' if imported else ''}.part"
        self._regular(path)
        return path

    def compatible(self, identifier, temporary=False):
        self._identifier(identifier)
        if self.configured:
            path = self.compatible_dir / f"{identifier}{'.tmp' if temporary else ''}.mp4"
        else:
            path = self.compatible_dir / f"{identifier}.compatible{'.tmp' if temporary else ''}.mp4"
        self._regular(path)
        return path

    def disk_usage(self):
        return shutil.disk_usage(self.root)

    def ensure_original_links(self, identifier, filename):
        if not self.configured:
            return self.original(identifier)
        folder = self.video_directory(identifier)
        stable, named = folder / ".source", folder / validate_filename(filename)
        stable_exists, named_exists = self._regular(stable), self._regular(named)
        if stable_exists and named_exists:
            if not self._same_file(stable, named):
                raise StorageConflict("Původní soubor a jeho stabilní odkaz nejsou shodné.")
        elif named_exists:
            os.link(named, stable, follow_symlinks=False)
            fsync_directory(folder)
        elif stable_exists:
            os.link(stable, named, follow_symlinks=False)
            fsync_directory(folder)
        else:
            raise StorageError("Původní soubor videa není dostupný na vyhrazeném disku.")
        return stable

    def publish_original(self, identifier, filename, staged=None):
        staged = staged or self.part(identifier)
        if self.configured:
            self.video_directory(identifier, create=True)
            target = self.named_original(identifier, filename)
        else:
            target = self.original(identifier)
        if self._regular(staged):
            try:
                os.link(staged, target, follow_symlinks=False)
                fsync_directory(target.parent)
            except FileExistsError:
                if not self._same_file(staged, target):
                    raise StorageConflict("Cíl pro originál už obsahuje jiný soubor.") from None
            if self.configured:
                # Keep the staged name until both destination hardlinks are durable.
                self.ensure_original_links(identifier, filename)
            staged.unlink()
            fsync_directory(staged.parent)
        elif not self._regular(target):
            raise StorageError("Nahraný originál není dostupný.")
        if self.configured:
            return self.ensure_original_links(identifier, filename)
        return target

    def recover_original(self, identifier, filename):
        """Recover an interrupted pre-catalog publication under the caller's lock."""
        stable = self.original(identifier)
        if self.configured and stable.parent.is_dir():
            named = self.named_original(identifier, filename)
            if self._regular(stable) or self._regular(named):
                return self.ensure_original_links(identifier, filename)
        return stable

    def remove_original(self, identifier, filename=None, rename_to=None):
        if not self.configured:
            self.original(identifier).unlink(missing_ok=True)
            return
        folder = self.originals_dir / identifier
        if not folder.exists():
            return
        self.video_directory(identifier)
        stable = folder / ".source"
        stable_stat = self._regular(stable)
        candidates = [folder / validate_filename(name) for name in dict.fromkeys((filename, rename_to)) if name]
        for candidate in candidates:
            self._regular(candidate)
            if candidate.exists() and stable_stat and not self._same_file(stable, candidate):
                raise StorageConflict("Originál koliduje s jiným souborem; nic se nemaže.")
        for candidate in candidates:
            candidate.unlink(missing_ok=True)
        stable.unlink(missing_ok=True)
        fsync_directory(folder)
        try:
            folder.rmdir()
        except OSError:
            pass  # Preserve any unrelated files deliberately placed in this directory.

    def _set_rename(self, connection, identifier, filename=None, pending=None):
        connection.execute("BEGIN IMMEDIATE")
        try:
            if filename is None:
                connection.execute("UPDATE videos SET rename_to=? WHERE id=?", (pending, identifier))
            else:
                connection.execute("UPDATE videos SET filename=?,rename_to=NULL WHERE id=?", (filename, identifier))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _finish_rename(self, connection, row):
        old, new, identifier = row["filename"], row["rename_to"], row["id"]
        if not new:
            return True
        validate_filename(new, old)
        if not self.configured:
            self._set_rename(connection, identifier, filename=new)
            return True
        folder = self.video_directory(identifier)
        stable = folder / ".source"
        old_path, new_path = folder / validate_filename(old), folder / new
        # The stable hardlink keeps streams and conversions valid throughout recovery.
        if not self._regular(stable):
            candidate = old_path if self._regular(old_path) else new_path
            if not self._regular(candidate):
                raise StorageError("Původní soubor pro obnovení přejmenování není dostupný.")
            os.link(candidate, stable, follow_symlinks=False)
            fsync_directory(folder)
        if self._regular(old_path) and not self._same_file(stable, old_path):
            raise StorageConflict("Původní název koliduje s jiným souborem.")
        try:
            os.link(stable, new_path, follow_symlinks=False)
            fsync_directory(folder)
        except FileExistsError:
            try:
                same = self._same_file(stable, new_path)
            except StorageConflict:
                same = False
            if not same:
                if not self._regular(old_path):
                    os.link(stable, old_path, follow_symlinks=False)
                    fsync_directory(folder)
                self._set_rename(connection, identifier, pending=None)
                return False
        if old_path != new_path and self._regular(old_path):
            if not self._same_file(stable, old_path):
                raise StorageConflict("Původní název se během přejmenování změnil.")
            old_path.unlink()
            fsync_directory(folder)
        self._set_rename(connection, identifier, filename=new)
        return True

    def rename(self, connection, identifier, filename):
        with self.file_lock(identifier):
            row = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise StorageError("Video neexistuje.")
            if row["rename_to"]:
                self._finish_rename(connection, row)
                row = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
            validate_filename(filename, row["filename"])
            if filename == row["filename"]:
                return
            if self.configured:
                self.ensure_original_links(identifier, row["filename"])
                target = self.named_original(identifier, filename)
                if target.exists() or target.is_symlink():
                    try:
                        same = self._same_file(self.original(identifier), target)
                    except StorageConflict:
                        same = False
                    if not same:
                        raise StorageConflict("Soubor s tímto názvem už existuje; nebyl přepsaný.")
            self._set_rename(connection, identifier, pending=filename)
            pending = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
            if not self._finish_rename(connection, pending):
                raise StorageConflict("Soubor s tímto názvem už existuje; nebyl přepsaný.")

    def recover(self, connection, pending_only=False):
        query = "SELECT * FROM videos WHERE rename_to IS NOT NULL" if pending_only or not self.configured else "SELECT * FROM videos"
        rows = connection.execute(query).fetchall()
        for stale in rows:
            with self.file_lock(stale["id"]):
                row = connection.execute("SELECT * FROM videos WHERE id=?", (stale["id"],)).fetchone()
                if not row:
                    continue
                if row["rename_to"]:
                    self._finish_rename(connection, row)
                    row = connection.execute("SELECT * FROM videos WHERE id=?", (stale["id"],)).fetchone()
                if self.configured:
                    self.ensure_original_links(row["id"], row["filename"])

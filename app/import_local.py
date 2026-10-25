"""Operator-only import of an explicitly completed, read-only source file.

Run only after the owner confirms the source upload is finished. This module
does not watch folders, delete sources, or expose any HTTP import endpoint.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid

BLOCK_SIZE = 8 * 1024**2
MAX_FILE_SIZE = 100 * 1024**3


class ImportErrorSafe(Exception):
    pass


def fingerprint(snapshot):
    return (snapshot.st_dev, snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns, snapshot.st_ctime_ns)


def ensure_source_unchanged(source_path, descriptor, initial):
    try:
        current = os.fstat(descriptor)
        named = source_path.stat(follow_symlinks=False)
    except OSError:
        raise ImportErrorSafe("Zdroj se během importu změnil nebo není dostupný. Import nebyl zveřejněn.") from None
    if fingerprint(current) != fingerprint(initial) or fingerprint(named) != fingerprint(initial):
        raise ImportErrorSafe("Zdroj se během importu změnil. Po dokončení nahrávání spusťte import znovu.")


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def import_local(source, title=None, description="", application=None):
    """Copy and verify a completed file, then publish it once by SHA-256."""
    if application is None:
        from server import app
        application = app
    internal = application.extensions["playnow"]
    data_dir = internal["data_dir"]
    storage = internal["storage"]
    source_path = Path(source)
    if not source_path.is_absolute():
        raise ImportErrorSafe("Zdroj musí být zadaný úplnou cestou.")
    try:
        resolved = source_path.resolve(strict=True)
    except OSError:
        raise ImportErrorSafe("Zdrojový soubor není dostupný.") from None
    if resolved.is_relative_to(data_dir) or (storage.configured and resolved.is_relative_to(storage.root)):
        raise ImportErrorSafe("Zdroj musí ležet mimo úložiště aplikace.")
    filename = source_path.name
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise ImportErrorSafe("Název zdrojového souboru není platný.")
    title = title.strip() if isinstance(title, str) else Path(filename).stem
    description = description.strip() if isinstance(description, str) else ""
    if not title or len(title) > 180 or len(description) > 3000 or any(ord(char) < 32 and char not in "\n\r\t" for char in title + description):
        raise ImportErrorSafe("Název nebo popis videa není platný.")
    identifier = uuid.uuid4().hex
    staging = storage.part(identifier, imported=True)
    target = storage.original(identifier)
    published = False
    source_descriptor = None
    with (data_dir / "locks" / "import-local").open("a+b") as singleton:
        try:
            fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImportErrorSafe("Jiný místní import právě běží.") from None
        try:
            source_descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            initial = os.fstat(source_descriptor)
            if not stat.S_ISREG(initial.st_mode) or not 1 <= initial.st_size <= MAX_FILE_SIZE:
                raise ImportErrorSafe("Zdroj musí být běžný dokončený soubor o velikosti 1 B až 100 GB.")
            ensure_source_unchanged(source_path, source_descriptor, initial)
            with application.app_context():
                connection = internal["db"]()

                def check_space(remaining):
                    reserved = connection.execute("SELECT COALESCE(SUM(size-offset),0) FROM uploads").fetchone()[0]
                    if storage.disk_usage().free - reserved - remaining < application.config["DISK_RESERVE_BYTES"]:
                        raise ImportErrorSafe("Pro import není dost místa včetně rozpracovaných nahrávání a bezpečné rezervy.")

                check_space(initial.st_size)
                digest = hashlib.sha256()
                copied = 0
                with staging.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    while copied < initial.st_size:
                        block = os.read(source_descriptor, min(BLOCK_SIZE, initial.st_size - copied))
                        if not block:
                            raise ImportErrorSafe("Zdrojový soubor není úplný. Import nebyl zveřejněn.")
                        check_space(initial.st_size - copied)
                        written = output.write(block)
                        if written != len(block):
                            raise ImportErrorSafe("Kopii souboru se nepodařilo úplně zapsat.")
                        digest.update(block)
                        copied += written
                    output.flush()
                    os.fsync(output.fileno())
                ensure_source_unchanged(source_path, source_descriptor, initial)
                source_hash = digest.hexdigest()
                if staging.stat().st_size != initial.st_size or hash_file(staging) != source_hash:
                    raise ImportErrorSafe("Kontrolní součet kopie nesouhlasí. Import nebyl zveřejněn.")
                ensure_source_unchanged(source_path, source_descriptor, initial)
                check_space(0)
                existing = connection.execute("SELECT * FROM videos WHERE source_sha256=?", (source_hash,)).fetchone()
                if existing:
                    original = storage.original(existing["id"])
                    if not original.is_file() or original.stat().st_size != initial.st_size:
                        raise ImportErrorSafe("Video se stejným kontrolním součtem už v katalogu je, ale jeho originál není dostupný.")
                    return {"id": existing["id"], "title": existing["title"], "status": existing["processing_status"], "source_sha256": source_hash, "imported": False}
                row = {"filename": filename, "title": title, "description": description, "size": initial.st_size}
                with internal["file_lock"](identifier):
                    ensure_source_unchanged(source_path, source_descriptor, initial)
                    target = storage.publish_original(identifier, filename, staged=staging)
                    result = internal["publish_media_record"](identifier, row, source_sha256=source_hash)
                    published = True
                return {"id": result["id"], "title": result["title"], "status": result["processing_status"], "source_sha256": source_hash, "imported": True}
        except ImportErrorSafe:
            raise
        except (OSError, ValueError):
            raise ImportErrorSafe("Místní import nebyl dokončen. Zdrojový soubor zůstává zachovaný.") from None
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            staging.unlink(missing_ok=True)
            if not published and target.exists():
                # A commit may have completed just before interruption. Preserve any
                # file already referenced by the catalog; on an unreadable DB, also
                # preserve it for operator recovery instead of risking data loss.
                try:
                    with application.app_context():
                        published = internal["db"]().execute("SELECT 1 FROM videos WHERE id=?", (identifier,)).fetchone() is not None
                except Exception:
                    published = True
            if not published:
                # On a normal failure remove only this invocation's generated files.
                # A successful idempotent return never points at this fresh identifier.
                storage.remove_original(identifier, filename)
                (data_dir / "posters" / f"{identifier}.jpg").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Import dokončeného místního videa; používejte až po potvrzení dokončení nahrávání.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--title")
    parser.add_argument("--description", default="")
    options = parser.parse_args()
    try:
        result = import_local(options.source, options.title, options.description)
    except ImportErrorSafe as exception:
        print(json.dumps({"error": str(exception)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "Import selhal před dokončením. Zdroj zůstává zachovaný."}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Sequential low-priority compatible-copy worker. The source file is never changed."""
import contextlib
import fcntl
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from storage import MediaStorage, StorageError, migrate_storage


VIDEO_COLUMNS = {
    "source_sha256": "TEXT",
    "compatible": "INTEGER NOT NULL DEFAULT 0",
    "processing_status": "TEXT NOT NULL DEFAULT 'none'",
    "processing_error": "TEXT",
    "source_codec": "TEXT",
    "source_width": "INTEGER",
    "source_height": "INTEGER",
    "source_fps": "REAL",
    "source_hdr": "INTEGER NOT NULL DEFAULT 0",
    "compatible_codec": "TEXT",
    "compatible_width": "INTEGER",
    "compatible_height": "INTEGER",
    "compatible_fps": "REAL",
    "compatible_hdr": "INTEGER NOT NULL DEFAULT 0",
    "compatible_size": "INTEGER NOT NULL DEFAULT 0",
}
FORMAT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi,mpeg,mpegts,ogg"]
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class MediaError(Exception):
    """A safe, user-facing processing error."""


def migrate_videos(connection):
    """Add columns transactionally while preserving all existing records."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(videos)")}
        if columns:
            for name, definition in VIDEO_COLUMNS.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS videos_source_sha256 ON videos(source_sha256) WHERE source_sha256 IS NOT NULL")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def finite_number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def parse_rate(value):
    try:
        result = float(Fraction(str(value)))
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def probe_media(path, timeout=12):
    executable = shutil.which("ffprobe")
    if not executable:
        raise MediaError("Na serveru chybí nástroj pro ověření videa.")
    try:
        result = subprocess.run([executable, "-v", "error", *FORMAT_OPTIONS,
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,pix_fmt,color_transfer,color_primaries,color_space,channels,duration",
            "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        payload = json.loads(result.stdout)
        video = next((item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None)
        if not video:
            raise MediaError("Soubor neobsahuje podporovanou obrazovou stopu.")
        audio = next((item for item in payload.get("streams", []) if item.get("codec_type") == "audio"), {})
        duration = finite_number(payload.get("format", {}).get("duration")) or finite_number(video.get("duration"))
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if width <= 0 or height <= 0:
            raise MediaError("Rozlišení zdrojového videa se nepodařilo zjistit.")
        return {
            "codec": str(video.get("codec_name", ""))[:40], "width": width, "height": height,
            "fps": parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")),
            "hdr": video.get("color_transfer") in ("smpte2084", "arib-std-b67"),
            "duration": duration if duration and duration > 0 else None,
            "pixel_format": video.get("pix_fmt"), "audio_codec": audio.get("codec_name"),
            "audio_channels": int(audio.get("channels", 0)),
        }
    except MediaError:
        raise
    except (subprocess.SubprocessError, ValueError, TypeError, OSError, StopIteration):
        raise MediaError("Formát zdrojového videa se nepodařilo bezpečně ověřit.") from None


def build_command(source, target, metadata):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise MediaError("Na serveru chybí nástroj pro přípravu videa.")
    filters = ["fps=60"] if (metadata.get("fps") or 0) > 60 else []
    if metadata.get("hdr"):
        filters.extend([
            "zscale=transfer=linear:npl=100", "format=gbrpf32le", "zscale=primaries=bt709",
            "tonemap=tonemap=hable:desat=0", "zscale=transfer=bt709:matrix=bt709:range=limited",
        ])
    filters.append("scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:out_color_matrix=bt709:out_range=tv")
    filters.append("format=yuv420p")
    command = [executable, "-nostdin", "-nostats", "-v", "error", "-xerror", "-threads", "1", *FORMAT_OPTIONS,
        "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1",
        "-vf", ",".join(filters), "-filter_threads", "1", "-filter_complex_threads", "1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "6M", "-bufsize", "12M",
        "-pix_fmt", "yuv420p", "-threads", "1", "-fpsmax", "60",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-ac", "2", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", "-y", str(target)]
    nice = shutil.which("nice")
    return [nice, "-n", "19", *command] if nice else command


def valid_compatible(metadata):
    return bool(metadata.get("codec") == "h264" and metadata.get("pixel_format") == "yuv420p"
        and not metadata.get("hdr") and 0 < metadata.get("width", 0) <= 1920
        and 0 < metadata.get("height", 0) <= 1080 and 0 < (metadata.get("fps") or 0) <= 60.1
        and (not metadata.get("audio_codec") or (metadata.get("audio_codec") == "aac" and 0 < metadata.get("audio_channels", 0) <= 2)))


class TranscodeWorker:
    def __init__(self, data_dir, reserve_bytes=10 * 1024**3, poll_seconds=2, timeout_seconds=48 * 3600, storage_dir=None):
        self.data_dir = Path(data_dir).resolve()
        self.database = self.data_dir / "catalog.sqlite3"
        self.storage = MediaStorage(self.data_dir, storage_dir)
        self.reserve_bytes = reserve_bytes
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.stopping = False
        self.process = None

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextlib.contextmanager
    def file_lock(self, identifier):
        if not ID_PATTERN.fullmatch(identifier):
            raise MediaError("Neplatný identifikátor videa.")
        with (self.data_dir / "locks" / identifier).open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def recover_processing(self):
        with contextlib.closing(self.connect()) as connection:
            migrate_videos(connection)
            migrate_storage(connection)
            self.storage.recover(connection)
            connection.execute("UPDATE videos SET processing_status='queued',processing_error=NULL WHERE processing_status='processing'")
        pattern = "*.tmp.mp4" if self.storage.configured else "*.compatible.tmp.mp4"
        for path in self.storage.compatible_dir.glob(pattern):
            if ID_PATTERN.fullmatch(path.name.split(".", 1)[0]):
                path.unlink(missing_ok=True)

    def claim_next(self):
        with contextlib.closing(self.connect()) as connection:
            self.storage.recover(connection, pending_only=True)
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM videos WHERE processing_status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    connection.execute("UPDATE videos SET processing_status='processing',processing_error=NULL WHERE id=? AND processing_status='queued'", (row["id"],))
                connection.execute("COMMIT")
                return dict(row) if row else None
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def space_available(self, needed=0):
        with contextlib.closing(self.connect()) as connection:
            remaining = connection.execute("SELECT COALESCE(SUM(size-offset),0) FROM uploads").fetchone()[0]
        return self.storage.disk_usage().free - remaining - needed >= self.reserve_bytes

    def still_exists(self, identifier):
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT processing_status FROM videos WHERE id=?", (identifier,)).fetchone()
            return bool(row and row["processing_status"] == "processing")

    def fail(self, identifier, message):
        with contextlib.closing(self.connect()) as connection:
            connection.execute("UPDATE videos SET processing_status='failed',processing_error=?,compatible=0 WHERE id=? AND processing_status='processing'", (message, identifier))

    def stop_process(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def publish(self, identifier, temporary, target, metadata):
        with self.file_lock(identifier), contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT id FROM videos WHERE id=? AND processing_status='processing'", (identifier,)).fetchone()
                if not row:
                    connection.execute("ROLLBACK")
                    return False
                if not self.space_available():
                    raise MediaError("Na disku nezbyla bezpečná rezerva. Originál je zachovaný.")
                if temporary != target:
                    os.replace(temporary, target)
                descriptor = os.open(target, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                folder = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
                connection.execute("UPDATE videos SET compatible=1,processing_status='ready',processing_error=NULL,compatible_width=?,compatible_height=?,compatible_fps=?,compatible_codec=?,compatible_hdr=0,compatible_size=? WHERE id=?", (metadata["width"], metadata["height"], metadata["fps"], metadata["codec"], target.stat().st_size, identifier))
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def process_next(self):
        row = self.claim_next()
        if not row:
            return False
        identifier = row["id"]
        if not ID_PATTERN.fullmatch(identifier):
            self.fail(identifier, "Neplatný identifikátor videa.")
            return True
        source = self.storage.original(identifier)
        temporary = self.storage.compatible(identifier, temporary=True)
        target = self.storage.compatible(identifier)
        try:
            with self.file_lock(identifier):
                if not self.still_exists(identifier):
                    return True
                if not source.is_file():
                    raise MediaError("Originální soubor není dostupný.")
            metadata = probe_media(source)
            with contextlib.closing(self.connect()) as connection:
                connection.execute("UPDATE videos SET source_width=?,source_height=?,source_fps=?,source_codec=?,source_hdr=?,duration=COALESCE(duration,?) WHERE id=?", (metadata["width"], metadata["height"], metadata["fps"], metadata["codec"], int(metadata["hdr"]), metadata["duration"], identifier))
            # Recover a crash between final rename and database publication without re-encoding.
            if target.is_file():
                try:
                    ready_metadata = probe_media(target)
                    if valid_compatible(ready_metadata):
                        self.publish(identifier, target, target, ready_metadata)
                        return True
                except MediaError:
                    pass
            estimated = int((metadata["duration"] * 6_160_000 / 8) if metadata.get("duration") else row["size"]) + 64 * 1024**2
            if not self.space_available(estimated):
                raise MediaError("Pro přehrávací kopii není dost volného místa včetně rezervy.")
            temporary.unlink(missing_ok=True)
            command = build_command(source, temporary, metadata)
            with tempfile.TemporaryFile() as diagnostics:
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostics)
                deadline = time.monotonic() + self.timeout_seconds
                while self.process.poll() is None:
                    if self.stopping:
                        self.stop_process()
                        # Leave 'processing' so the next worker startup can resume the queue.
                        return True
                    if not self.still_exists(identifier):
                        self.stop_process()
                        return True
                    if time.monotonic() > deadline:
                        raise MediaError("Příprava videa překročila časový limit. Originál je zachovaný.")
                    if not self.space_available():
                        raise MediaError("Příprava byla zastavena kvůli nedostatku místa. Originál je zachovaný.")
                    time.sleep(self.poll_seconds)
                if self.process.returncode != 0:
                    raise MediaError("Přehrávací kopii se nepodařilo vytvořit. Originál je zachovaný.")
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise MediaError("Přehrávací kopie není úplná. Originál je zachovaný.")
            compatible_metadata = probe_media(temporary)
            if not valid_compatible(compatible_metadata):
                raise MediaError("Přehrávací kopie neprošla kontrolou H.264 / 1080p / 60 snímků za sekundu.")
            self.publish(identifier, temporary, target, compatible_metadata)
        except MediaError as exception:
            self.stop_process()
            self.fail(identifier, str(exception))
        except (OSError, sqlite3.Error, subprocess.SubprocessError, StorageError):
            self.stop_process()
            self.fail(identifier, "Příprava videa byla přerušena chybou serveru. Originál je zachovaný.")
        finally:
            self.process = None
            temporary.unlink(missing_ok=True)
        return True

    def run(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in ("media", "locks"):
            (self.data_dir / folder).mkdir(exist_ok=True, mode=0o700)
        with (self.data_dir / "locks" / "transcode-worker").open("a+b") as singleton:
            try:
                fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("A compatible-copy worker is already running") from None
            self.recover_processing()
            while not self.stopping:
                if not self.process_next():
                    time.sleep(self.poll_seconds)


def main():
    if os.environ.get("PLAYNOW_TRANSCODE_ENABLED", "0") != "1":
        raise SystemExit("PLAYNOW_TRANSCODE_ENABLED=1 is required")
    data_dir = os.environ.get("PLAYNOW_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    worker = TranscodeWorker(data_dir, reserve_bytes=int(os.environ.get("PLAYNOW_DISK_RESERVE_BYTES", str(10 * 1024**3))), storage_dir=os.environ.get("PLAYNOW_STORAGE_DIR") or None)
    def stop(_signum, _frame):
        worker.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()


if __name__ == "__main__":
    main()

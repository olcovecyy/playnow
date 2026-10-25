"""Security, integrity and recovery checks using isolated temporary data only."""
import concurrent.futures
import importlib.util
import os
import sys
import json
import sqlite3
import hashlib
import time
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "app"))
BOOTSTRAP = tempfile.TemporaryDirectory(prefix="playnow-import-")
VIEWER_HASH = generate_password_hash("viewer-test-password", method="scrypt")
ADMIN_HASH = generate_password_hash("admin-test-password", method="scrypt")
os.environ["PLAYNOW_SECRET_KEY"] = "temporary-test-secret-key-over-32-characters"
os.environ["PLAYNOW_VIEWER_PASSWORD_HASH"] = VIEWER_HASH
os.environ["PLAYNOW_ADMIN_PASSWORD_HASH"] = ADMIN_HASH
os.environ["PLAYNOW_DATA_DIR"] = BOOTSTRAP.name
os.environ["PLAYNOW_STORAGE_DIR"] = ""
spec = importlib.util.spec_from_file_location("playnow_server", HERE / "app" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
import transcode
import import_local as local_import
import storage as media_storage


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="playnow-test-")
        self.config = dict(
            TESTING=True,
            SECRET_KEY="persistent-test-key-with-more-than-32-characters",
            VIEWER_PASSWORD_HASH=VIEWER_HASH,
            ADMIN_PASSWORD_HASH=ADMIN_HASH,
            DATA_DIR=self.temp.name,
            STORAGE_DIR=None,
            COOKIE_SECURE=False,
            SESSION_COOKIE_SECURE=False,
            PUBLIC_URL="",
            PROCESS_MEDIA=False,
            TRANSCODE_ENABLED=False,
            DISK_RESERVE_BYTES=0,
        )
        self.app = server.create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, role="admin", client=None):
        client = client or self.client
        state = client.get("/api/session").get_json()
        response = client.post("/api/login", json={"role": role, "password": f"{role}-test-password"}, headers={"X-CSRF-Token": state["csrf"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["csrf"]

    def create(self, csrf, content=b"0123456789abcdef", filename="family.mp4", modified=123):
        response = self.client.post("/api/uploads", json={"filename": filename, "size": len(content), "title": "Rodinné video", "description": "Soukromá vzpomínka", "last_modified": modified}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def chunk(self, identifier, csrf, content, offset=0, client=None):
        return (client or self.client).put(f"/api/uploads/{identifier}", data=content, headers={"X-CSRF-Token": csrf, "Upload-Offset": str(offset), "Content-Type": "application/octet-stream"})

    def publish(self, content=b"0123456789abcdef"):
        csrf = self.login()
        identifier = self.create(csrf, content)
        self.assertEqual(self.chunk(identifier, csrf, content).status_code, 200)
        response = self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 201, response.get_json())
        return identifier, csrf

    def test_catalog_and_direct_media_are_protected(self):
        identifier, _csrf = self.publish()
        anonymous = self.app.test_client()
        for route in ("/api/videos", f"/media/{identifier}", f"/poster/{identifier}"):
            self.assertEqual(anonymous.get(route).status_code, 401)
        self.assertEqual(anonymous.get(f"/static/../data/media/{identifier}.video").status_code, 404)
        self.login("viewer", anonymous)
        response = anonymous.get(f"/media/{identifier}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"0123456789abcdef")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()

    def test_csrf_foreign_origin_and_admin_restriction(self):
        state = self.client.get("/api/session").get_json()
        self.assertEqual(self.client.post("/api/login", json={"role": "admin", "password": "admin-test-password"}).status_code, 403)
        self.assertEqual(self.client.post("/api/login", json={"role": "admin", "password": "admin-test-password"}, headers={"X-CSRF-Token": state["csrf"], "Origin": "https://evil.example"}).status_code, 403)
        csrf = self.login("viewer")
        self.assertEqual(self.client.get("/api/uploads").status_code, 403)
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "x.mp4", "size": 10}, headers={"X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.delete("/api/videos/" + "a" * 32, headers={"X-CSRF-Token": csrf}).status_code, 403)

    def test_interrupted_upload_resumes_after_restart_without_duplication(self):
        csrf = self.login()
        content = b"first-part--second-part"
        identifier = self.create(csrf, content)
        split = 12
        first = self.chunk(identifier, csrf, content[:split])
        self.assertEqual(first.get_json()["offset"], split)
        self.assertEqual(self.client.get("/api/videos").get_json()["videos"], [])
        self.assertEqual(self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf}).status_code, 409)
        second_app = server.create_app(self.config)
        resumed = second_app.test_client()
        cookie = self.client.get_cookie("playnow_session")
        resumed.set_cookie("playnow_session", cookie.value)
        current = resumed.get(f"/api/uploads/{identifier}").get_json()
        self.assertEqual(current["offset"], split)
        repeated = resumed.post("/api/uploads", json={"filename": "family.mp4", "size": len(content), "title": "Rodinné video", "last_modified": 123}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(repeated.get_json()["id"], identifier)
        self.assertEqual(repeated.get_json()["offset"], split)
        stale = self.chunk(identifier, csrf, content[:split], client=resumed)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["offset"], split)
        self.assertEqual(self.chunk(identifier, csrf, content[split:], offset=split, client=resumed).status_code, 200)
        completed = resumed.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf})
        self.assertEqual(completed.status_code, 201)
        self.assertEqual(resumed.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(resumed.get("/api/uploads").get_json()["uploads"], [])
        result = resumed.get(f"/media/{identifier}")
        self.assertEqual(result.data, content)
        result.close()

    def test_stream_ranges_have_exact_bytes_and_invalid_ranges_fail(self):
        identifier, _csrf = self.publish()
        response = self.client.get(f"/media/{identifier}", headers={"Range": "bytes=2-6"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"23456")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-6/16")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        response.close()
        suffix = self.client.get(f"/media/{identifier}", headers={"Range": "bytes=-4"})
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.data, b"cdef")
        suffix.close()
        for requested in ("bytes=99-100", "bytes=5-2", "bytes=0-2,5-8"):
            invalid = self.client.get(f"/media/{identifier}", headers={"Range": requested})
            self.assertEqual(invalid.status_code, 416)
            self.assertEqual(invalid.headers["Content-Range"], "bytes */16")

    def test_parallel_duplicate_chunks_do_not_corrupt_upload(self):
        csrf = self.login()
        content = b"onechunk"
        identifier = self.create(csrf, content)
        cookie = self.client.get_cookie("playnow_session").value
        gate = threading.Barrier(2)

        def submit():
            client = self.app.test_client()
            client.set_cookie("playnow_session", cookie)
            gate.wait(timeout=5)
            return self.chunk(identifier, csrf, content, client=client).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _value: submit(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        part = Path(self.temp.name) / "uploads" / f"{identifier}.part"
        self.assertEqual(part.read_bytes(), content)
        self.assertEqual(self.client.get(f"/api/uploads/{identifier}").get_json()["offset"], len(content))

    def test_full_eight_mib_chunk_plus_remainder(self):
        csrf = self.login()
        content = b"12345678" * (server.CHUNK_SIZE // 8) + b"end"
        identifier = self.create(csrf, content)
        response = self.chunk(identifier, csrf, content[:server.CHUNK_SIZE])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["offset"], server.CHUNK_SIZE)
        self.assertEqual(self.chunk(identifier, csrf, content[server.CHUNK_SIZE:], offset=server.CHUNK_SIZE).status_code, 200)
        self.assertEqual(self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf}).status_code, 201)
        response = self.client.get(f"/media/{identifier}", headers={"Range": f"bytes={server.CHUNK_SIZE - 2}-{server.CHUNK_SIZE + 2}"})
        self.assertEqual(response.data, b"78end")
        response.close()

    def test_recovers_fsynced_chunk_and_interrupted_atomic_publication(self):
        csrf = self.login()
        content = b"recover-me"
        identifier = self.create(csrf, content)
        part = Path(self.temp.name) / "uploads" / f"{identifier}.part"
        # Simulate a killed worker after fsync but before the SQLite commit.
        part.write_bytes(content)
        state = self.client.get(f"/api/uploads/{identifier}").get_json()
        self.assertEqual(state["offset"], len(content))
        target = Path(self.temp.name) / "media" / f"{identifier}.video"
        part.replace(target)
        # Simulate a killed worker after rename but before publishing metadata.
        response = self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(len(self.client.get("/api/videos").get_json()["videos"]), 1)

    def test_logout_revokes_copied_session_and_rotates_csrf(self):
        identifier, csrf = self.publish()
        copied = self.app.test_client()
        copied.set_cookie("playnow_session", self.client.get_cookie("playnow_session").value)
        response = self.client.post("/api/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json()["csrf"], csrf)
        self.assertEqual(copied.get(f"/media/{identifier}").status_code, 401)

    def test_login_limit_persists_across_application_restart(self):
        self.app.config["LOGIN_LIMIT"] = 3
        state = self.client.get("/api/session").get_json()
        for _index in range(3):
            response = self.client.post("/api/login", json={"role": "admin", "password": "incorrect"}, headers={"X-CSRF-Token": state["csrf"]})
            self.assertEqual(response.status_code, 401)
        config = self.config | {"LOGIN_LIMIT": 3}
        restarted = server.create_app(config).test_client()
        new_state = restarted.get("/api/session").get_json()
        response = restarted.post("/api/login", json={"role": "admin", "password": "admin-test-password"}, headers={"X-CSRF-Token": new_state["csrf"]})
        self.assertEqual(response.status_code, 429)

    def test_disk_reservations_payload_limits_and_delete(self):
        csrf = self.login()
        self.app.config["DISK_RESERVE_BYTES"] = 10
        with patch.object(server.shutil, "disk_usage", return_value=types.SimpleNamespace(free=25, total=100)):
            response = self.client.post("/api/uploads", json={"filename": "too-large.mp4", "size": 16}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 507)
            identifier = self.create(csrf, b"1234567890")
            second = self.client.post("/api/uploads", json={"filename": "second.mp4", "size": 10}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(second.status_code, 507)
        oversized = self.chunk(identifier, csrf, b"a" * (server.CHUNK_SIZE + 1))
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.client.delete(f"/api/uploads/{identifier}", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertFalse((Path(self.temp.name) / "uploads" / f"{identifier}.part").exists())

    def test_public_host_validation_and_malformed_input(self):
        configured = server.create_app(self.config | {"PUBLIC_URL": "https://videos.example"}).test_client()
        self.assertEqual(configured.get("/api/session").status_code, 400)
        self.assertEqual(configured.get("/api/session", base_url="https://videos.example").status_code, 200)
        csrf = self.login()
        response = self.client.post("/api/uploads", json=["not", "an", "object"], headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 400)

    def update_video(self, identifier, **values):
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        try:
            fields = ",".join(f"{key}=?" for key in values)
            connection.execute(f"UPDATE videos SET {fields} WHERE id=?", (*values.values(), identifier))
            connection.commit()
        finally:
            connection.close()

    def test_compatible_variant_preference_fallback_and_source_preservation(self):
        identifier, csrf = self.publish()
        unavailable = self.client.get(f"/media/{identifier}?quality=compatible")
        self.assertEqual(unavailable.status_code, 409)
        item = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(item["processing_status"], "none")
        self.assertIsNone(item["compatible_url"])
        self.assertIn("quality=original", item["original_url"])
        compatible = Path(self.temp.name) / "media" / f"{identifier}.compatible.mp4"
        compatible.write_bytes(b"small-compatible-copy")
        self.update_video(identifier, compatible=1, processing_status="ready", source_width=3840, source_height=2160, source_fps=120, source_codec="hevc", source_hdr=1, compatible_width=1920, compatible_height=1080, compatible_fps=60, compatible_codec="h264")
        item = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(item["compatibility"]["width"], 1920)
        self.assertEqual(item["source_fps"], 120)
        self.assertTrue(item["source_hdr"])
        for path in (f"/media/{identifier}", item["compatible_url"]):
            response = self.client.get(path, headers={"Range": "bytes=0-4"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b"small")
            response.close()
        response = self.client.get(item["original_url"])
        self.assertEqual(response.data, b"0123456789abcdef")
        response.close()
        compatible.unlink()
        missing_copy = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(missing_copy["processing_status"], "failed")
        self.assertIsNone(missing_copy["compatible_url"])
        self.assertFalse(missing_copy["compatible"])
        response = self.client.get(f"/media/{identifier}")
        self.assertEqual(response.data, b"0123456789abcdef")
        response.close()
        self.assertEqual(self.client.get(f"/media/{identifier}?quality=compatible").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/videos/{identifier}", headers={"X-CSRF-Token": csrf}).status_code, 200)

    def test_queue_prepare_retry_permissions_and_status(self):
        self.app.config["TRANSCODE_ENABLED"] = True
        identifier, csrf = self.publish()
        endpoint = f"/api/videos/{identifier}/prepare"
        first = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(first["processing_status"], "queued")
        self.assertEqual(self.client.post(endpoint, headers={"X-CSRF-Token": csrf}).get_json()["video"]["processing_status"], "queued")
        self.update_video(identifier, processing_status="processing")
        self.assertEqual(self.client.post(endpoint, headers={"X-CSRF-Token": csrf}).status_code, 409)
        self.update_video(identifier, processing_status="failed", processing_error="temporary failure")
        response = self.client.post(endpoint, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["video"]["processing_status"], "queued")
        self.assertIsNone(response.get_json()["video"]["compatibility"]["error"])
        viewer = self.app.test_client()
        viewer_csrf = self.login("viewer", viewer)
        self.assertEqual(viewer.post(endpoint, headers={"X-CSRF-Token": viewer_csrf}).status_code, 403)
        self.app.config["TRANSCODE_ENABLED"] = False
        self.assertEqual(self.client.post(endpoint, headers={"X-CSRF-Token": csrf}).status_code, 503)

    def test_existing_database_migration_preserves_video(self):
        identifier, _csrf = self.publish()
        # Recreate just the historical table shape, then start the upgraded application.
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        connection.execute("ALTER TABLE videos RENAME TO videos_new")
        connection.execute("CREATE TABLE videos AS SELECT id,filename,title,description,size,created_at,mime,duration,poster FROM videos_new")
        connection.execute("DROP TABLE videos_new")
        connection.commit()
        connection.close()
        server.create_app(self.config)
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        try:
            row = connection.execute("SELECT id,compatible,processing_status FROM videos").fetchone()
            self.assertEqual(row, (identifier, 0, "none"))
        finally:
            connection.close()

    def test_worker_command_caps_fps_and_maps_hdr_without_upscaling(self):
        with patch.object(transcode.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            command = transcode.build_command("/data/source.video", "/data/output.mp4", {"fps": 120, "hdr": True})
            self.assertEqual(command[:3], ["/usr/bin/nice", "-n", "19"])
            self.assertIsInstance(command, list)
            filters = command[command.index("-vf") + 1]
            self.assertTrue(filters.startswith("fps=60,"))
            self.assertIn("tonemap=tonemap=hable", filters)
            self.assertIn("min(1920,iw)", filters)
            self.assertIn("min(1080,ih)", filters)
            self.assertIn("force_divisible_by=2", filters)
            self.assertEqual(command[command.index("-c:v") + 1], "libx264")
            self.assertEqual(command[command.index("-ac") + 1], "2")
            self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
            for index, part in enumerate(command):
                if part in ("-threads", "-filter_threads", "-filter_complex_threads"):
                    self.assertEqual(command[index + 1], "1")
            low_fps = transcode.build_command("/data/source.video", "/data/output.mp4", {"fps": 24, "hdr": False})
            low_filters = low_fps[low_fps.index("-vf") + 1]
            self.assertNotIn("fps=60", low_filters)
            self.assertNotIn("tonemap", low_filters)
        for transfer in ("smpte2084", "arib-std-b67"):
            payload = {"format": {"duration": "3.0"}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "avg_frame_rate": "0/0", "r_frame_rate": "120/1", "color_transfer": transfer}]}
            with patch.object(transcode.shutil, "which", return_value="/ffprobe"), patch.object(transcode.subprocess, "run", return_value=types.SimpleNamespace(stdout=json.dumps(payload).encode())):
                metadata = transcode.probe_media("/data/source.video")
            self.assertEqual(metadata["fps"], 120)
            self.assertTrue(metadata["hdr"])

    def test_worker_publishes_copy_recovers_queue_and_honors_delete(self):
        self.app.config["TRANSCODE_ENABLED"] = True
        identifier, csrf = self.publish()
        source_path = Path(self.temp.name) / "media" / f"{identifier}.video"
        original = source_path.read_bytes()
        worker = transcode.TranscodeWorker(self.temp.name, reserve_bytes=0, poll_seconds=0)
        self.update_video(identifier, processing_status="processing")
        worker.recover_processing()
        source_metadata = dict(codec="hevc", width=3840, height=2160, fps=120, hdr=True, duration=1, pixel_format="yuv420p10le", audio_codec=None, audio_channels=0)
        compatible_metadata = source_metadata | dict(codec="h264", width=1920, height=1080, fps=60, hdr=False, pixel_format="yuv420p")

        def probe(path, **_options):
            return compatible_metadata if "compatible" in str(path) else source_metadata

        def finished_process(command, **_options):
            Path(command[-1]).write_bytes(b"compatible-video")
            return types.SimpleNamespace(poll=lambda: 0, returncode=0)

        with patch.object(transcode, "probe_media", side_effect=probe), patch.object(transcode, "build_command", side_effect=lambda source, target, metadata: ["fake-ffmpeg", str(target)]), patch.object(transcode.subprocess, "Popen", side_effect=finished_process), patch.object(transcode.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1024**3, total=1024**3)):
            self.assertTrue(worker.process_next())
        item = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(item["processing_status"], "ready")
        self.assertEqual(item["compatibility"]["fps"], 60)
        self.assertEqual(source_path.read_bytes(), original)
        compatible_path = source_path.with_name(f"{identifier}.compatible.mp4")
        self.assertEqual(compatible_path.read_bytes(), b"compatible-video")
        self.assertFalse(worker.process_next())

        # A delete during encoding must remain deleted, with no ghost output published.
        self.update_video(identifier, processing_status="queued", compatible=0)
        compatible_path.unlink()
        canceled = {"terminated": False}

        def deleted_process(command, **_options):
            Path(command[-1]).write_bytes(b"discard-this")
            response = self.client.delete(f"/api/videos/{identifier}", headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200)
            return types.SimpleNamespace(
                poll=lambda: -15 if canceled["terminated"] else None,
                terminate=lambda: canceled.update(terminated=True),
                wait=lambda timeout: -15,
                returncode=None,
            )

        with patch.object(transcode, "probe_media", side_effect=probe), patch.object(transcode, "build_command", side_effect=lambda source, target, metadata: ["fake-ffmpeg", str(target)]), patch.object(transcode.subprocess, "Popen", side_effect=deleted_process), patch.object(transcode.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1024**3, total=1024**3)):
            self.assertTrue(worker.process_next())
        self.assertTrue(canceled["terminated"])
        self.assertFalse(compatible_path.exists())
        self.assertFalse(source_path.exists())
        self.assertFalse(source_path.with_name(f"{identifier}.compatible.tmp.mp4").exists())
        self.assertEqual(self.client.get("/api/videos").get_json()["videos"], [])

    def test_worker_failure_preserves_original_and_disk_reserve(self):
        self.app.config["TRANSCODE_ENABLED"] = True
        identifier, _csrf = self.publish()
        metadata = dict(codec="hevc", width=3840, height=2160, fps=120, hdr=True, duration=300, pixel_format="yuv420p10le", audio_codec=None, audio_channels=0)
        worker = transcode.TranscodeWorker(self.temp.name, reserve_bytes=10, poll_seconds=0)
        with patch.object(transcode, "probe_media", return_value=metadata), patch.object(transcode.shutil, "disk_usage", return_value=types.SimpleNamespace(free=50, total=100)), patch.object(transcode.subprocess, "Popen") as process:
            self.assertTrue(worker.process_next())
            process.assert_not_called()
        item = self.client.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(item["processing_status"], "failed")
        self.assertIn("místa", item["compatibility"]["error"])
        self.assertEqual((Path(self.temp.name) / "media" / f"{identifier}.video").read_bytes(), b"0123456789abcdef")

    def test_local_import_verifies_source_preserves_it_and_is_idempotent(self):
        self.app.config["TRANSCODE_ENABLED"] = True
        with tempfile.TemporaryDirectory(prefix="playnow-source-") as source_folder:
            source = Path(source_folder) / "sample-family-video.MOV"
            content = b"completed-source-file" * 500
            source.write_bytes(content)
            original_stat = source.stat()
            first = local_import.import_local(source, title="Rodinné video", application=self.app)
            second = local_import.import_local(source, title="Nemá přepsat původní název", application=self.app)
            self.assertTrue(first["imported"])
            self.assertFalse(second["imported"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["title"], "Rodinné video")
            self.assertEqual(first["status"], "queued")
            self.assertEqual(len(first["source_sha256"]), 64)
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual((Path(self.temp.name) / "media" / f"{first['id']}.video").read_bytes(), content)
            csrf = self.login()
            self.assertEqual(len(self.client.get("/api/videos").get_json()["videos"]), 1)
            self.assertEqual(self.client.get("/api/uploads").get_json()["uploads"], [])

    def test_local_import_rejects_source_changed_before_publication(self):
        with tempfile.TemporaryDirectory(prefix="playnow-growing-source-") as source_folder:
            source = Path(source_folder) / "still-uploading.MOV"
            source.write_bytes(b"initial-uploaded-bytes")
            original_hash_file = local_import.hash_file

            def change_source_during_verification(path):
                result = original_hash_file(path)
                with source.open("ab") as output:
                    output.write(b"new-bytes-arrived")
                return result

            with patch.object(local_import, "hash_file", side_effect=change_source_during_verification):
                with self.assertRaises(local_import.ImportErrorSafe):
                    local_import.import_local(source, application=self.app)
            self.assertEqual(source.read_bytes(), b"initial-uploaded-bytesnew-bytes-arrived")
            self.assertEqual(list((Path(self.temp.name) / "media").glob("*.video")), [])
            self.assertEqual(list((Path(self.temp.name) / "uploads").glob("*.import.part")), [])
            self.login()
            self.assertEqual(self.client.get("/api/videos").get_json()["videos"], [])

    def test_local_import_rejects_internal_source_and_insufficient_space(self):
        internal_source = Path(self.temp.name) / "source.MOV"
        internal_source.write_bytes(b"private-data-file")
        with self.assertRaises(local_import.ImportErrorSafe):
            local_import.import_local(internal_source, application=self.app)
        with tempfile.TemporaryDirectory(prefix="playnow-source-space-") as source_folder:
            source = Path(source_folder) / "complete.MOV"
            source.write_bytes(b"1234567890")
            self.app.config["DISK_RESERVE_BYTES"] = 10
            with patch.object(local_import.shutil, "disk_usage", return_value=types.SimpleNamespace(free=15, total=100)):
                with self.assertRaises(local_import.ImportErrorSafe):
                    local_import.import_local(source, application=self.app)
            self.assertEqual(source.read_bytes(), b"1234567890")
            self.assertEqual(list((Path(self.temp.name) / "uploads").glob("*.import.part")), [])

    def make_share(self, identifier, csrf):
        response = self.client.post(f"/api/videos/{identifier}/shares", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        parsed = urlsplit(payload["url"])
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parse_qs(parsed.query), {"video": [identifier]})
        token = parse_qs(parsed.fragment)["access"][0]
        self.assertGreaterEqual(len(token), 43)
        self.assertNotIn(token, parsed.query)
        return payload["share"], token

    def redeem(self, token, client=None):
        client = client or self.app.test_client()
        state = client.get("/api/session").get_json()
        response = client.post("/api/share/redeem", json={"token": token}, headers={"X-CSRF-Token": state["csrf"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        return client, response.get_json()

    def test_shared_session_only_sees_target_and_preserves_range_variants(self):
        first, _old_csrf = self.publish()
        second, csrf = self.publish(b"another-private-video")
        share, token = self.make_share(first, csrf)
        scoped, grant = self.redeem(token)
        self.assertEqual(grant["role"], "viewer")
        self.assertEqual(grant["video_id"], first)
        self.assertEqual(scoped.get("/api/session").get_json()["video_id"], first)
        catalog = scoped.get("/api/videos").get_json()
        self.assertEqual([item["id"] for item in catalog["videos"]], [first])
        self.assertIsNone(catalog["storage"])
        for path in (f"/media/{second}", f"/media/{second}?quality=original", f"/poster/{second}"):
            self.assertEqual(scoped.get(path).status_code, 403)
        for path in ("/api/uploads", f"/api/videos/{first}/shares"):
            self.assertEqual(scoped.get(path).status_code, 403)
        self.assertEqual(scoped.post(f"/api/videos/{first}/shares", headers={"X-CSRF-Token": grant["csrf"]}).status_code, 403)
        self.assertEqual(scoped.delete(f"/api/videos/{first}/shares/{share['id']}", headers={"X-CSRF-Token": grant["csrf"]}).status_code, 403)
        self.assertEqual(scoped.patch(f"/api/videos/{first}", json={"title": "must fail"}, headers={"X-CSRF-Token": grant["csrf"]}).status_code, 403)
        compatible = Path(self.temp.name) / "media" / f"{first}.compatible.mp4"
        compatible.write_bytes(b"compatible-copy-bytes")
        self.update_video(first, compatible=1, processing_status="ready")
        for path, expected in ((f"/media/{first}?quality=original", b"23456"), (f"/media/{first}?quality=compatible", b"mpati")):
            response = scoped.get(path, headers={"Range": "bytes=2-6"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, expected)
            response.close()
        invalid = scoped.get(f"/media/{first}?quality=original", headers={"Range": "bytes=999-1000"})
        self.assertEqual(invalid.status_code, 416)

    def test_share_revoke_invalidates_existing_and_copied_cookies(self):
        identifier, csrf = self.publish()
        share, token = self.make_share(identifier, csrf)
        scoped, _grant = self.redeem(token)
        copied = self.app.test_client()
        copied.set_cookie("playnow_session", scoped.get_cookie("playnow_session").value)
        endpoint = f"/api/videos/{identifier}/shares/{share['id']}"
        self.assertEqual(self.client.delete(endpoint, headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(self.client.delete(endpoint, headers={"X-CSRF-Token": csrf}).status_code, 200)
        for client in (scoped, copied):
            self.assertEqual(client.get("/api/videos").status_code, 401)
            self.assertEqual(client.get(f"/media/{identifier}").status_code, 401)
            self.assertIsNone(client.get("/api/session").get_json()["role"])
        revoked_state = scoped.get("/api/session").get_json()
        self.assertEqual(scoped.post("/api/share/redeem", json={"token": token}, headers={"X-CSRF-Token": revoked_state["csrf"]}).status_code, 401)
        listing = self.client.get(f"/api/videos/{identifier}/shares").get_json()["shares"]
        self.assertIsNotNone(listing[0]["revoked_at"])
        self.assertEqual(set(listing[0]), {"id", "created_at", "revoked_at"})
        self.assertNotIn(token, json.dumps(listing))
        self.assertEqual(self.client.get("/api/session").get_json()["role"], "admin")

    def test_share_hash_only_persistence_and_expired_scoped_session(self):
        identifier, _csrf = self.publish()
        second, csrf = self.publish(b"second")
        share, token = self.make_share(identifier, csrf)
        scoped, _grant = self.redeem(token)
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        try:
            stored = connection.execute("SELECT token_hash FROM video_shares WHERE id=?", (share["id"],)).fetchone()[0]
            self.assertEqual(stored, hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(stored, token)
            stored_role = connection.execute("SELECT role FROM sessions WHERE share_id=?", (share["id"],)).fetchone()[0]
            self.assertEqual(stored_role, "share_viewer")
            self.assertNotIn(stored_role, ("viewer", "admin"))
        finally:
            connection.close()
        for path in Path(self.temp.name).glob("catalog.sqlite3*"):
            self.assertNotIn(token.encode(), path.read_bytes())
        restarted = server.create_app(self.config).test_client()
        restarted.set_cookie("playnow_session", scoped.get_cookie("playnow_session").value)
        self.assertEqual([item["id"] for item in restarted.get("/api/videos").get_json()["videos"]], [identifier])
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        connection.execute("UPDATE sessions SET expires=? WHERE share_id=?", (time.time() - 1, share["id"]))
        connection.commit()
        connection.close()
        self.assertEqual(restarted.get(f"/media/{identifier}").status_code, 401)
        self.assertIsNone(restarted.get("/api/session").get_json()["role"])
        # The link itself has no expiry: a fresh redemption grants a new bounded session.
        restarted, state = self.redeem(token, restarted)
        self.assertEqual(state["video_id"], identifier)
        self.assertEqual(restarted.get(f"/media/{second}").status_code, 403)
        deleted = self.client.delete(f"/api/videos/{identifier}", headers={"X-CSRF-Token": csrf})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(restarted.get("/api/videos").status_code, 401)
        fresh = self.app.test_client()
        state = fresh.get("/api/session").get_json()
        self.assertEqual(fresh.post("/api/share/redeem", json={"token": token}, headers={"X-CSRF-Token": state["csrf"]}).status_code, 401)

    def test_share_redeem_requires_origin_csrf_and_valid_token(self):
        identifier, csrf = self.publish()
        _share, token = self.make_share(identifier, csrf)
        guest = self.app.test_client()
        state = guest.get("/api/session").get_json()
        self.assertEqual(guest.post("/api/share/redeem", json={"token": token}).status_code, 403)
        self.assertEqual(guest.post("/api/share/redeem", json={"token": token}, headers={"X-CSRF-Token": state["csrf"], "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(guest.post("/api/share/redeem", json={"token": token}, headers={"X-CSRF-Token": state["csrf"], "Sec-Fetch-Site": "cross-site"}).status_code, 403)
        for malformed in (None, "", "short", "a" * 129, ["wrong"], "a" * 42 + "+"):
            response = guest.post("/api/share/redeem", json={"token": malformed}, headers={"X-CSRF-Token": state["csrf"]})
            self.assertEqual(response.status_code, 400)
        unknown = guest.post("/api/share/redeem", json={"token": "a" * 43}, headers={"X-CSRF-Token": state["csrf"]})
        self.assertEqual(unknown.status_code, 401)
        self.assertIsNone(guest.get("/api/session").get_json()["role"])

    def test_share_redemption_preserves_existing_full_viewer_or_admin(self):
        identifier, _old_csrf = self.publish()
        _second, csrf = self.publish(b"second-private")
        _share, token = self.make_share(identifier, csrf)
        admin, state = self.redeem(token, self.client)
        self.assertEqual(state["role"], "admin")
        self.assertEqual(state["video_id"], identifier)
        self.assertIsNone(admin.get("/api/session").get_json()["video_id"])
        self.assertEqual(len(admin.get("/api/videos").get_json()["videos"]), 2)
        viewer = self.app.test_client()
        self.login("viewer", viewer)
        viewer, state = self.redeem(token, viewer)
        self.assertEqual(state["role"], "viewer")
        self.assertEqual(state["video_id"], identifier)
        self.assertIsNone(viewer.get("/api/session").get_json()["video_id"])
        self.assertEqual(len(viewer.get("/api/videos").get_json()["videos"]), 2)
        self.assertIsNotNone(viewer.get("/api/videos").get_json()["storage"])

    def test_share_migration_preserves_old_full_sessions_across_initializers(self):
        csrf = self.login("viewer")
        cookie = self.client.get_cookie("playnow_session").value
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        connection.execute("ALTER TABLE sessions RENAME TO sessions_new")
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY,role TEXT,csrf TEXT NOT NULL,expires REAL NOT NULL)")
        connection.execute("INSERT INTO sessions SELECT id,role,csrf,expires FROM sessions_new")
        connection.execute("DROP TABLE sessions_new")
        connection.commit()
        connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            applications = list(pool.map(lambda _item: server.create_app(self.config), range(2)))
        migrated = applications[0].test_client()
        migrated.set_cookie("playnow_session", cookie)
        state = migrated.get("/api/session").get_json()
        self.assertEqual(state["role"], "viewer")
        self.assertEqual(state["csrf"], csrf)
        self.assertIsNone(state["video_id"])
        self.assertEqual(migrated.get("/api/videos").status_code, 200)

    def test_upload_fingerprint_resumes_changed_mtime_without_merging_different_bytes(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        metadata = {"filename": "IMG_0001.MOV", "size": 16, "title": "iPhone video", "last_modified": 12345, "content_fingerprint": "a" * 64}
        first = self.client.post("/api/uploads", json=metadata, headers=headers)
        self.assertEqual(first.status_code, 201)
        identifier = first.get_json()["id"]
        self.assertEqual(self.chunk(identifier, csrf, b"firstpart").status_code, 200)
        resumed = self.client.post("/api/uploads", json=metadata | {"last_modified": 0}, headers=headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["id"], identifier)
        self.assertEqual(resumed.get_json()["offset"], 9)
        self.assertEqual(resumed.get_json()["content_fingerprint"], "a" * 64)
        different = self.client.post("/api/uploads", json=metadata | {"content_fingerprint": "b" * 64}, headers=headers)
        self.assertEqual(different.status_code, 201)
        self.assertNotEqual(different.get_json()["id"], identifier)
        self.assertEqual(different.get_json()["offset"], 0)
        legacy_metadata = {"filename": "legacy.MOV", "size": 16, "last_modified": 555}
        legacy = self.client.post("/api/uploads", json=legacy_metadata, headers=headers)
        modern = self.client.post("/api/uploads", json=legacy_metadata | {"content_fingerprint": "c" * 64}, headers=headers)
        self.assertEqual(modern.status_code, 201)
        self.assertNotEqual(legacy.get_json()["id"], modern.get_json()["id"])
        self.assertEqual(self.client.get(f"/api/uploads/{identifier}").get_json()["content_fingerprint"], "a" * 64)

    def test_upload_unknown_timestamps_never_automerge_and_invalid_hashes_fail(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        for index, value in enumerate((None, 0, False, True, -1, float("nan"), float("inf"), "0", "123")):
            metadata = {"filename": f"IMG_{index:04}.MOV", "size": 4, "last_modified": value}
            first = self.client.post("/api/uploads", json=metadata, headers=headers)
            second = self.client.post("/api/uploads", json=metadata, headers=headers)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(second.status_code, 201)
            self.assertNotEqual(first.get_json()["id"], second.get_json()["id"])
            self.assertEqual(first.get_json()["last_modified"], "")
        for malformed in ("", "a" * 63, "A" * 64, "g" * 64, 123, False, {}, []):
            response = self.client.post("/api/uploads", json={"filename": "IMG.MOV", "size": 4, "content_fingerprint": malformed}, headers=headers)
            self.assertEqual(response.status_code, 400)

    def test_upload_fingerprint_additive_migration_preserves_legacy_resume(self):
        csrf = self.login()
        identifier = self.create(csrf, b"0123456789abcdef", filename="old-iPhone.MOV", modified=12345)
        self.assertEqual(self.chunk(identifier, csrf, b"0123").status_code, 200)
        cookie = self.client.get_cookie("playnow_session").value
        connection = sqlite3.connect(Path(self.temp.name) / "catalog.sqlite3")
        connection.execute("ALTER TABLE uploads RENAME TO uploads_new")
        connection.execute("CREATE TABLE uploads (id TEXT PRIMARY KEY,filename TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,size INTEGER NOT NULL,offset INTEGER NOT NULL DEFAULT 0,last_modified TEXT NOT NULL,created_at REAL NOT NULL)")
        connection.execute("INSERT INTO uploads SELECT id,filename,title,description,size,offset,last_modified,created_at FROM uploads_new")
        connection.execute("DROP TABLE uploads_new")
        connection.commit()
        connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            applications = list(pool.map(lambda _item: server.create_app(self.config), range(2)))
        migrated = applications[0].test_client()
        migrated.set_cookie("playnow_session", cookie)
        old = migrated.get(f"/api/uploads/{identifier}").get_json()
        self.assertEqual(old["offset"], 4)
        self.assertIsNone(old["content_fingerprint"])
        self.assertEqual(old["last_modified"], "12345")
        resumed = migrated.post("/api/uploads", json={"filename": "old-iPhone.MOV", "size": 16, "last_modified": 12345}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["id"], identifier)
        self.assertEqual(resumed.get_json()["offset"], 4)

    def configured_storage(self):
        media_temp = tempfile.TemporaryDirectory(prefix="playnow-media-disk-")
        self.addCleanup(media_temp.cleanup)
        root = Path(media_temp.name).resolve()
        (root / ".playnow-storage").write_bytes(media_storage.MARKER_CONTENT)
        self.config["STORAGE_DIR"] = str(root)
        self.app = server.create_app(self.config)
        self.client = self.app.test_client()
        return root

    def test_configured_storage_web_upload_uses_media_disk_and_blocks_public_upload(self):
        root = self.configured_storage()
        anonymous = self.app.test_client()
        guest = anonymous.get("/api/session").get_json()
        self.assertEqual(anonymous.get("/api/uploads").status_code, 401)
        self.assertEqual(anonymous.post("/api/uploads", json={"filename": "IMG.MOV", "size": 8}, headers={"X-CSRF-Token": guest["csrf"]}).status_code, 401)
        viewer_csrf = self.login("viewer", anonymous)
        self.assertEqual(anonymous.post("/api/uploads", json={"filename": "IMG.MOV", "size": 8}, headers={"X-CSRF-Token": viewer_csrf}).status_code, 403)
        csrf = self.login()
        content = b"real-video-bytes-preserved" * 100
        identifier = self.create(csrf, content, filename="IMG_0001.MOV")
        self.assertTrue((root / "Nahravani" / f"{identifier}.part").is_file())
        self.assertFalse((Path(self.temp.name) / "uploads" / f"{identifier}.part").exists())
        self.assertEqual(self.chunk(identifier, csrf, content).status_code, 200)
        result = self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf})
        self.assertEqual(result.status_code, 201, result.get_json())
        named = root / "Originaly" / identifier / "IMG_0001.MOV"
        stable = named.with_name(".source")
        self.assertEqual(named.read_bytes(), content)
        self.assertEqual(named.stat().st_ino, stable.stat().st_ino)
        self.assertEqual(named.stat().st_dev, stable.stat().st_dev)
        self.assertFalse((root / "Nahravani" / f"{identifier}.part").exists())
        self.assertFalse((Path(self.temp.name) / "media" / f"{identifier}.video").exists())
        response = self.client.get(f"/media/{identifier}?quality=original", headers={"Range": "bytes=1-8"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, content[1:9])
        self.assertEqual(response.mimetype, "video/quicktime")
        response.close()

    def test_configured_capacity_is_from_media_volume_and_marker_fails_closed(self):
        root = self.configured_storage()
        csrf = self.login()
        self.app.config["DISK_RESERVE_BYTES"] = 10
        def disk(path):
            return types.SimpleNamespace(free=15 if Path(path) == root else 1024**3, total=100)
        with patch.object(media_storage.shutil, "disk_usage", side_effect=disk):
            listing = self.client.get("/api/videos").get_json()
            self.assertEqual(listing["storage"]["free"], 15)
            response = self.client.post("/api/uploads", json={"filename": "disk.MOV", "size": 6}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 507)
        missing = root / "not-mounted"
        with self.assertRaises(media_storage.StorageError):
            server.create_app(self.config | {"STORAGE_DIR": str(missing)})
        self.assertFalse(missing.exists())
        (root / ".playnow-storage").write_bytes(b"wrong-disk\n")
        with self.assertRaises(media_storage.StorageError):
            server.create_app(self.config)

    def test_configured_filename_rename_is_real_and_share_links_remain_valid(self):
        root = self.configured_storage()
        identifier, csrf = self.publish()
        share, token = self.make_share(identifier, csrf)
        scoped, _state = self.redeem(token)
        folder = root / "Originaly" / identifier
        original_inode = (folder / ".source").stat().st_ino
        response = self.client.patch(f"/api/videos/{identifier}", json={"filename": "Rodinná oslava.MP4"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["video"]["filename"], "Rodinná oslava.MP4")
        self.assertEqual(response.get_json()["video"]["title"], "Rodinné video")
        self.assertFalse((folder / "family.mp4").exists())
        self.assertEqual((folder / "Rodinná oslava.MP4").read_bytes(), b"0123456789abcdef")
        self.assertEqual((folder / ".source").stat().st_ino, original_inode)
        restart = server.create_app(self.config).test_client()
        restart.set_cookie("playnow_session", scoped.get_cookie("playnow_session").value)
        video = restart.get("/api/videos").get_json()["videos"][0]
        self.assertEqual(video["id"], identifier)
        self.assertEqual(video["original_url"], f"/media/{identifier}?quality=original")
        result = restart.get(video["original_url"], headers={"Range": "bytes=0-3"})
        self.assertEqual(result.data, b"0123")
        self.assertEqual(result.status_code, 206)
        result.close()
        self.assertEqual(scoped.patch(f"/api/videos/{identifier}", json={"filename": "unauthorized.mp4"}, headers={"X-CSRF-Token": _state["csrf"]}).status_code, 403)

    def test_configured_filename_validation_and_no_overwrite_or_symlink(self):
        root = self.configured_storage()
        identifier, csrf = self.publish()
        endpoint = f"/api/videos/{identifier}"
        headers = {"X-CSRF-Token": csrf}
        for invalid in ("../bad.mp4", "/bad.mp4", "bad\\file.mp4", ".hidden.mp4", "bad\x7f.mp4", "bad\x85.mp4", "bad.mov", "", " bad.mp4", "a" * 237 + ".mp4"):
            self.assertEqual(self.client.patch(endpoint, json={"filename": invalid}, headers=headers).status_code, 400)
        folder = root / "Originaly" / identifier
        other = folder / "existing.mp4"
        other.write_bytes(b"unrelated-preserve-me")
        self.assertEqual(self.client.patch(endpoint, json={"filename": other.name}, headers=headers).status_code, 409)
        self.assertEqual(other.read_bytes(), b"unrelated-preserve-me")
        outside = root / "outside-file"
        outside.write_bytes(b"outside-preserve-me")
        symlink = folder / "link.mp4"
        symlink.symlink_to(outside)
        self.assertEqual(self.client.patch(endpoint, json={"filename": symlink.name}, headers=headers).status_code, 409)
        self.assertEqual(outside.read_bytes(), b"outside-preserve-me")
        self.assertEqual((folder / "family.mp4").read_bytes(), b"0123456789abcdef")

    def test_configured_rename_journal_recovers_each_crash_boundary(self):
        root = self.configured_storage()
        identifier, csrf = self.publish()
        folder = root / "Originaly" / identifier
        stable = folder / ".source"
        inode = stable.stat().st_ino
        current = "family.mp4"
        for stage, target in (("intent", "first.mp4"), ("both-links", "second.mp4"), ("old-unlinked", "third.mp4")):
            self.update_video(identifier, rename_to=target)
            if stage != "intent":
                os.link(stable, folder / target)
            if stage == "old-unlinked":
                (folder / current).unlink()
            restarted = server.create_app(self.config)
            client = restarted.test_client()
            client.set_cookie("playnow_session", self.client.get_cookie("playnow_session").value)
            result = client.get("/api/videos").get_json()["videos"][0]
            self.assertEqual(result["filename"], target)
            self.assertFalse((folder / current).exists())
            self.assertEqual((folder / target).stat().st_ino, inode)
            self.assertEqual(stable.read_bytes(), b"0123456789abcdef")
            current = target
        unrelated = folder / "collision.mp4"
        unrelated.write_bytes(b"unrelated-content")
        self.update_video(identifier, rename_to=unrelated.name)
        restarted = server.create_app(self.config).test_client()
        restarted.set_cookie("playnow_session", self.client.get_cookie("playnow_session").value)
        self.assertEqual(restarted.get("/api/videos").get_json()["videos"][0]["filename"], current)
        self.assertEqual(unrelated.read_bytes(), b"unrelated-content")
        self.assertEqual((folder / current).stat().st_ino, inode)

    def test_configured_upload_recovers_named_original_without_stable_link(self):
        root = self.configured_storage()
        csrf = self.login()
        content = b"original-survives-interrupted-publication"
        identifier = self.create(csrf, content, filename="interrupted.MOV")
        self.assertEqual(self.chunk(identifier, csrf, content).status_code, 200)
        folder = root / "Originaly" / identifier
        folder.mkdir(mode=0o700)
        # Reproduce the historical crash boundary: stage already moved, .source absent.
        staged = root / "Nahravani" / f"{identifier}.part"
        named = folder / "interrupted.MOV"
        staged.replace(named)
        self.app = server.create_app(self.config)
        cookie = self.client.get_cookie("playnow_session").value
        self.client = self.app.test_client()
        self.client.set_cookie("playnow_session", cookie)
        state = self.client.get(f"/api/uploads/{identifier}")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.get_json()["offset"], len(content))
        self.assertFalse(staged.exists())
        self.assertEqual((folder / ".source").stat().st_ino, named.stat().st_ino)
        completed = self.client.post(f"/api/uploads/{identifier}/complete", headers={"X-CSRF-Token": csrf})
        self.assertEqual(completed.status_code, 201, completed.get_json())
        self.assertEqual(named.read_bytes(), content)

    def test_configured_import_and_worker_keep_all_media_on_media_volume(self):
        root = self.configured_storage()
        self.app.config["TRANSCODE_ENABLED"] = True
        with tempfile.TemporaryDirectory(prefix="playnow-external-source-") as source_folder:
            source = Path(source_folder) / "source.MOV"
            source.write_bytes(b"immutable-source" * 50)
            result = local_import.import_local(source, application=self.app)
        identifier = result["id"]
        stable = root / "Originaly" / identifier / ".source"
        original = stable.read_bytes()
        worker = transcode.TranscodeWorker(self.temp.name, reserve_bytes=0, poll_seconds=0, storage_dir=str(root))
        worker.recover_processing()
        source_metadata = dict(codec="hevc", width=3840, height=2160, fps=120, hdr=True, duration=1, pixel_format="yuv420p10le", audio_codec=None, audio_channels=0)
        compatible_metadata = source_metadata | dict(codec="h264", width=1920, height=1080, fps=60, hdr=False, pixel_format="yuv420p")
        def probe(path, **_options):
            return compatible_metadata if Path(path).parent == root / "Prehravani" else source_metadata
        def process(command, **_options):
            Path(command[-1]).write_bytes(b"compatible-media-volume")
            return types.SimpleNamespace(poll=lambda: 0, returncode=0)
        with patch.object(transcode, "probe_media", side_effect=probe), patch.object(transcode, "build_command", side_effect=lambda source, target, metadata: ["fake-ffmpeg", str(target)]), patch.object(transcode.subprocess, "Popen", side_effect=process), patch.object(media_storage.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1024**3, total=1024**3)):
            self.assertTrue(worker.process_next())
        self.assertEqual((root / "Prehravani" / f"{identifier}.mp4").read_bytes(), b"compatible-media-volume")
        self.assertEqual(stable.read_bytes(), original)
        self.assertFalse((Path(self.temp.name) / "media" / f"{identifier}.compatible.mp4").exists())
        self.assertFalse((root / "Prehravani" / f"{identifier}.tmp.mp4").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        BOOTSTRAP.cleanup()

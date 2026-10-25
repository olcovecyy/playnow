#!/usr/bin/env python3
"""Create a fresh PlayNow deployment using only the Python standard library."""
import argparse
import getpass
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import string
import sys
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent.parent
MARKER = b"playnow-storage-v1\n"


def password_hash(password):
    """Werkzeug-compatible scrypt:32768:8:1 with a random 16-character salt."""
    salt = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("ascii"), n=32768, r=8, p=1, maxmem=128 * 1024**2, dklen=64)
    return "scrypt:32768:8:1$" + salt + "$" + derived.hex()


def quote(value):
    value = str(value)
    if any(char in value for char in ("\n", "\r", "\x00")) or value.endswith("\\"):
        raise ValueError("Configuration values cannot contain newlines/NUL or end in a backslash.")
    return "'" + value.replace("'", "\\'") + "'"


def public_origin(value, port):
    parsed = urlsplit(value or f"http://localhost:{port}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("--public-url must be an HTTP(S) origin without credentials, a path, query or fragment.")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in hostname.split(".")):
            raise ValueError("The public hostname is invalid.") from None
        loopback = hostname == "localhost"
        formatted = hostname
    else:
        loopback = address.is_loopback
        formatted = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Use HTTPS for access beyond localhost.")
    selected_port = parsed.port
    default_port = 443 if parsed.scheme == "https" else 80
    host = formatted + (f":{selected_port}" if selected_port and selected_port != default_port else "")
    return f"{parsed.scheme}://{host}", host, selected_port or default_port


def read_password(label, path):
    if path is not None:
        path = Path(path).expanduser()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid():
            raise ValueError(f"{label} password file must be a regular, owner-only file belonging to this user (0600 or 0400).")
        if info.st_size > 8192:
            raise ValueError(f"{label} password file is too large.")
        password = path.read_text(encoding="utf-8")
        if password.endswith("\n"):
            password = password[:-1]
        if password.endswith("\r"):
            password = password[:-1]
    else:
        if not sys.stdin.isatty():
            raise ValueError("Interactive passwords require a terminal; use the two --*-password-file options for automation.")
        password = getpass.getpass(f"{label} password (at least 12 characters): ")
        if password != getpass.getpass(f"Confirm {label.lower()} password: "):
            raise ValueError(f"{label} passwords do not match.")
    if not 12 <= len(password) <= 1024 or not password.strip() or any(char in password for char in ("\n", "\r", "\x00")):
        raise ValueError(f"{label} password must contain 12–1024 characters without newlines/NUL.")
    return password


def destination(value, default):
    requested = Path(value).expanduser() if value else default
    if requested.is_symlink():
        raise ValueError("Data/media paths cannot be symbolic links.")
    path = requested.resolve()
    if path.exists():
        raise ValueError(f"Use a NEW dedicated directory; refusing existing path: {path}")
    if path == PROJECT or PROJECT in path.parents and PROJECT / "var" not in path.parents:
        raise ValueError("Within the repository, private directories must be under var/ (which is ignored by Git).")
    quote(path)
    return path


def create_directory(path, uid, gid, created):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise ValueError(f"Parent is not a directory: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created.append(directory)
        if os.geteuid() == 0:
            os.chown(directory, uid, gid)


def write_new(path, content, uid, gid):
    with path.open("x", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        if os.geteuid() == 0:
            os.fchown(output.fileno(), uid, gid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-url", help="Exact outside origin; default http://localhost:8080 (or --port).")
    parser.add_argument("--port", type=int, default=8080, help="Loopback HTTP port for the web container (default 8080).")
    parser.add_argument("--data-dir", help="NEW private metadata directory; default ./var/data.")
    parser.add_argument("--media-dir", help="NEW media directory, preferably on your large disk; default ./var/media.")
    parser.add_argument("--uid", type=int, default=os.getuid() if hasattr(os, "getuid") else 0)
    parser.add_argument("--gid", type=int, default=os.getgid() if hasattr(os, "getgid") else 0)
    parser.add_argument("--viewer-password-file", help="Automation only: read viewer password from an owner-only file.")
    parser.add_argument("--admin-password-file", help="Automation only: read admin password from an owner-only file.")
    args = parser.parse_args(argv)
    try:
        if os.name != "posix":
            raise ValueError("Run setup on Linux, macOS, or inside WSL2.")
        if args.uid <= 0 or args.gid <= 0:
            raise ValueError("Use a non-root UID/GID. Run without sudo, or explicitly supply the intended non-root --uid and --gid.")
        if os.geteuid() != 0 and (args.uid, args.gid) != (os.getuid(), os.getgid()):
            raise ValueError("Without root, --uid/--gid must match your current account; existing directories are never chowned.")
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535.")
        for name in (".env", "runtime.env"):
            if (PROJECT / name).exists() or (PROJECT / name).is_symlink():
                raise ValueError(f"{name} already exists; setup will not replace credentials or an existing deployment.")
        data = destination(args.data_dir, PROJECT / "var/data")
        media = destination(args.media_dir, PROJECT / "var/media")
        if data == media or data in media.parents or media in data.parents:
            raise ValueError("Data and media must be separate directories, neither inside the other.")
        origin, domain, https_port = public_origin(args.public_url, args.port)
        viewer = read_password("Viewer", args.viewer_password_file)
        admin = read_password("Admin", args.admin_password_file)
        if secrets.compare_digest(viewer.encode("utf-8"), admin.encode("utf-8")):
            raise ValueError("Choose different viewer and admin passwords.")
        # Hash before changing the filesystem; no password is printed or persisted.
        viewer_hash, admin_hash = password_hash(viewer), password_hash(admin)
        del viewer, admin
        runtime = {
            "PLAYNOW_SECRET_KEY": secrets.token_urlsafe(48),
            "PLAYNOW_VIEWER_PASSWORD_HASH": viewer_hash,
            "PLAYNOW_ADMIN_PASSWORD_HASH": admin_hash,
            "PLAYNOW_PUBLIC_URL": origin,
            "PLAYNOW_COOKIE_SECURE": "1" if origin.startswith("https://") else "0",
            "PLAYNOW_TRUSTED_PROXY_COUNT": "1" if origin.startswith("https://") else "0",
            "PLAYNOW_SESSION_HOURS": "168",
            "PLAYNOW_DISK_RESERVE_BYTES": str(10 * 1024**3),
            "PLAYNOW_PROCESS_MEDIA": "1",
        }
        compose = {"PLAYNOW_DATA_PATH": str(data), "PLAYNOW_MEDIA_PATH": str(media), "PLAYNOW_UID": args.uid,
                   "PLAYNOW_GID": args.gid, "PLAYNOW_PORT": args.port, "PLAYNOW_DOMAIN": domain if origin.startswith("https://") else "",
                   "PLAYNOW_HTTPS_PORT": https_port if origin.startswith("https://") else 443}
        runtime_text = "# Private. Generated by scripts/setup.py. Keep values single-quoted.\n" + "".join(f"{key}={quote(value)}\n" for key, value in runtime.items())
        compose_text = "# Generated Compose settings. Private runtime values are in runtime.env.\n" + "".join(f"{key}={quote(value)}\n" for key, value in compose.items())
        os.umask(0o077)
        created = []
        create_directory(data, args.uid, args.gid, created)
        create_directory(media, args.uid, args.gid, created)
        for name in ("Originaly", "Prehravani", "Nahravani"):
            create_directory(media / name, args.uid, args.gid, created)
        write_new(media / ".playnow-storage", MARKER.decode("ascii"), args.uid, args.gid)
        write_new(PROJECT / "runtime.env", runtime_text, args.uid, args.gid)
        write_new(PROJECT / ".env", compose_text, args.uid, args.gid)
        for path in reversed(created):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(PROJECT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        print("PlayNow configuration created. Passwords were hashed and were not saved or printed.")
        print(f"Private data: {data}\nMedia: {media}\nPublic origin: {origin}")
        if origin.startswith("https://"):
            print("With the included HTTPS proxy: docker compose -f compose.yaml -f deploy/compose.https.yaml up -d --build")
            print("With an existing HTTPS reverse proxy: docker compose up -d --build")
        else:
            print("Start: docker compose up -d --build")
            print(f"Open {origin} in your browser. /admin uses your separate admin password.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

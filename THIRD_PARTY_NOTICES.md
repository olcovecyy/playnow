# Third-party components

The PlayNow application source is provided under the [MIT license](LICENSE).
That license does not replace the licenses of the runtime, installed packages,
media tools, operating-system components, or user-supplied videos.

The repository references dependencies through package installation; it does
not vendor their source or binaries. Installed distributions retain their own
copyright and license files. Preserve those files when redistributing an image
or a packaged installation.

## Python application dependencies

| Component | Use | Upstream license information |
| --- | --- | --- |
| Flask 3.1.3 | HTTP application | [BSD-3-Clause](https://github.com/pallets/flask/blob/3.1.3/LICENSE.txt) |
| Gunicorn 26.2.0 | WSGI server | [MIT](https://github.com/benoitc/gunicorn/blob/26.2.0/LICENSE) |
| Werkzeug | HTTP utilities and password hashing | [BSD-3-Clause](https://github.com/pallets/werkzeug/blob/main/LICENSE.txt) |
| Jinja2 | Flask dependency | [BSD-3-Clause](https://github.com/pallets/jinja/blob/main/LICENSE.txt) |
| MarkupSafe | Flask dependency | [BSD-3-Clause](https://github.com/pallets/markupsafe/blob/main/LICENSE.txt) |
| ItsDangerous | Flask session signing | [BSD-3-Clause](https://github.com/pallets/itsdangerous/blob/main/LICENSE.txt) |
| Click | Flask dependency | [BSD-3-Clause](https://github.com/pallets/click/blob/main/LICENSE.txt) |
| Blinker | Flask dependency | [MIT](https://github.com/pallets-eco/blinker/blob/main/LICENSE.txt) |
| Packaging | Gunicorn dependency | [Apache-2.0 or BSD-2-Clause](https://github.com/pypa/packaging/blob/main/LICENSE) |

Transitive dependencies are resolved by the package installer. Inspect the
actual installed versions and their distribution metadata for a specific
build. License texts normally reside in each package's `.dist-info` directory,
including its `licenses` subdirectory on newer packages.

## Runtime and media processing

- [Python](https://docs.python.org/3/license.html) carries the Python Software
  Foundation license and additional notices for bundled components.
- [SQLite](https://sqlite.org/copyright.html), used through Python's standard
  library, is dedicated to the public domain by its authors.
- [FFmpeg](https://ffmpeg.org/legal.html) is a separate executable invoked for
  metadata, thumbnails, and compatible playback copies. Its license depends on
  the build: LGPL-2.1-or-later applies to the base project, while enabled GPL
  components change the applicable FFmpeg license. This project uses the
  `libx264` encoder when creating H.264 copies; do not assume that the resulting
  distribution package is LGPL-only.
- The Dockerfile installs the distribution's FFmpeg package. Its codecs and
  supporting libraries have their own licenses. Inspect `ffmpeg -version`,
  `ffmpeg -L`, and the package copyright files under `/usr/share/doc/` in the
  actual image. When redistributing binaries or images, preserve applicable
  notices and satisfy the corresponding source-distribution obligations for
  the exact binaries; this overview is not a source offer or a replacement for
  those materials.
- The base container and operating-system packages retain their respective
  licenses. The build recipe does not relicense them as MIT.
- [Caddy](https://github.com/caddyserver/caddy/blob/master/LICENSE), used by the
  optional HTTPS Compose configuration, is provided under Apache-2.0. Its
  official container and Go dependencies retain their own notices.

The frontend uses browser APIs and system-font stacks. Font names in CSS do not
mean that font files are bundled. Private videos, generated posters, and other
deployment data are not part of this source distribution. Operators remain
responsible for the material they upload and share.

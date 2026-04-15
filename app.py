import json
import os
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

# --- Configuration ---
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

_started_monotonic = time.monotonic()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def list_upload_folder():
    if not os.path.isdir(UPLOAD_FOLDER):
        return []
    return sorted(
        f
        for f in os.listdir(UPLOAD_FOLDER)
        if not f.startswith(".")
        and os.path.isfile(os.path.join(UPLOAD_FOLDER, f))
        and not f.endswith(".meta.json")
        and f != "_set_unique.meta.json"
    )


def load_lmstudio_sidecar(upload_path: Path):
    """Read `imagename.meta.json` written by analyze_uploads_people_lmstudio.py, or None."""
    side = upload_path.parent / (upload_path.name + ".meta.json")
    if not side.is_file():
        return None
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _resolved_upload_target(filename: str):
    """Return Path to file inside UPLOAD_FOLDER, or None if invalid or missing."""
    base = Path(app.config["UPLOAD_FOLDER"]).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def _exif_value_to_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        if len(val) > 96:
            return f"<binary data, {len(val)} bytes>"
        return val.decode("utf-8", errors="replace")
    try:
        from PIL.TiffImagePlugin import IFDRational

        if isinstance(val, IFDRational):
            return str(float(val))
    except ImportError:
        pass
    if isinstance(val, (tuple, list)):
        parts = [_exif_value_to_str(v) for v in val[:12]]
        return ", ".join(p for p in parts if p)
    s = str(val)
    return s[:420] + ("…" if len(s) > 420 else "")


def build_metadata_rows(path: Path):
    """General file info plus image/EXIF when Pillow can read the file."""
    st = path.stat()
    general = [
        ("File name", path.name),
        ("Size on disk", f"{st.st_size:,} bytes"),
        (
            "Last modified",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        ),
    ]
    ext = path.suffix.lower().lstrip(".")
    if ext not in IMAGE_EXTENSIONS:
        return {
            "general": general,
            "image": [],
            "exif": [],
            "is_image": False,
            "note": None,
        }

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        return {
            "general": general,
            "image": [],
            "exif": [],
            "is_image": True,
            "note": "Install Pillow (`pip install pillow`) to read image metadata and EXIF.",
        }

    image_rows = []
    exif_rows = []
    note = None

    try:
        with Image.open(path) as im:
            image_rows.extend(
                [
                    ("Format", im.format or "—"),
                    ("Dimensions", f"{im.width} × {im.height} px"),
                    ("Color mode", im.mode),
                ]
            )
            dpi = im.info.get("dpi")
            if (
                dpi
                and isinstance(dpi, (tuple, list))
                and len(dpi) >= 2
                and dpi[0]
                and dpi[1]
            ):
                image_rows.append(("DPI", f"{dpi[0]} × {dpi[1]}"))

            exif = im.getexif()
            if exif is not None:
                for tag_id, val in exif.items():
                    tname = TAGS.get(tag_id, f"Tag_{tag_id}")
                    if tname in ("MakerNote", "PrintImageMatching"):
                        continue
                    s = _exif_value_to_str(val)
                    if s:
                        exif_rows.append((tname, s))

                try:
                    from PIL.ExifTags import IFD
                except ImportError:
                    IFD = None
                if IFD is not None:
                    try:
                        gps_ifd = exif.get_ifd(IFD.GPSInfo)
                    except Exception:
                        gps_ifd = {}
                    if gps_ifd:
                        from PIL.ExifTags import GPSTAGS

                        for tag_id, val in gps_ifd.items():
                            tname = GPSTAGS.get(tag_id, f"Tag_{tag_id}")
                            s = _exif_value_to_str(val)
                            if s:
                                exif_rows.append((f"GPS {tname}", s))
    except Exception as exc:
        note = f"Could not read image metadata: {exc}"
    else:
        if not exif_rows:
            note = (
                "No EXIF block found. Phone/camera JPEGs usually include EXIF; "
                "exports, screenshots, and some web images strip it."
            )

    exif_rows.sort(key=lambda p: p[0].lower())

    return {
        "general": general,
        "image": image_rows,
        "exif": exif_rows,
        "is_image": True,
        "note": note,
    }


def _normalize_view_mode(raw) -> str:
    if not raw:
        return "thumb"
    v = str(raw).strip().lower()
    if v in ("thumb", "thumbnails", "thumbnail", "grid"):
        return "thumb"
    if v == "list":
        return "list"
    return "thumb"


HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0f1419">
  <title>File Uploader</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1419;
      --bg-elevated: #1a2332;
      --bg-input: #243044;
      --border: #2d3a4d;
      --text: #e7ecf3;
      --text-muted: #8b9aad;
      --accent: #5b9fd4;
      --accent-hover: #7eb6e8;
      --radius: 12px;
      --tap-min: 44px;
      --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      font-family: var(--font);
      font-size: 1rem;
      line-height: 1.5;
      color: var(--text);
      background: var(--bg);
      -webkit-font-smoothing: antialiased;
      padding:
        max(1rem, env(safe-area-inset-top))
        max(1rem, env(safe-area-inset-right))
        max(1.25rem, env(safe-area-inset-bottom))
        max(1rem, env(safe-area-inset-left));
    }
    .wrap {
      width: 100%;
      max-width: 28rem;
      margin: 0 auto;
    }
    header { margin-bottom: 1.5rem; }
    h1 {
      font-size: clamp(1.35rem, 4vw, 1.6rem);
      font-weight: 600;
      letter-spacing: -0.02em;
      margin: 0 0 0.35rem;
    }
    .lede {
      margin: 0;
      font-size: 0.9rem;
      color: var(--text-muted);
    }
    .card {
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem;
      margin-bottom: 1.5rem;
    }
    .upload-form { margin: 0; display: flex; flex-direction: column; gap: 0.75rem; }
    .file-row {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    @media (min-width: 380px) {
      .file-row {
        flex-direction: row;
        align-items: stretch;
        flex-wrap: wrap;
      }
      .file-row input[type="file"] { flex: 1 1 8rem; min-width: 0; }
    }
    input[type="file"] {
      min-height: var(--tap-min);
      padding: 0.5rem 0.65rem;
      font-size: 1rem;
      color: var(--text);
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: calc(var(--radius) - 4px);
      width: 100%;
    }
    input[type="submit"] {
      min-height: var(--tap-min);
      padding: 0 1.1rem;
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      color: var(--bg);
      background: var(--accent);
      border: none;
      border-radius: calc(var(--radius) - 4px);
      cursor: pointer;
      width: 100%;
    }
    @media (min-width: 380px) {
      input[type="submit"] { width: auto; align-self: flex-end; min-width: 7.5rem; }
    }
    input[type="submit"]:hover { background: var(--accent-hover); }
    input[type="submit"]:active { transform: scale(0.98); }
    .hint {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin: 0;
    }
    h2 {
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin: 0 0 0.65rem;
    }
    .file-link {
      display: flex;
      align-items: center;
      min-height: var(--tap-min);
      padding: 0.65rem 0.85rem;
      border-radius: calc(var(--radius) - 4px);
      border: 1px solid var(--border);
      background: var(--bg-input);
      color: var(--accent);
      text-decoration: none;
      word-break: break-word;
    }
    .file-link:hover { color: var(--accent-hover); border-color: var(--accent); }
    .file-link:active { opacity: 0.92; }
    .file-item {
      display: flex;
      align-items: stretch;
      gap: 0.5rem;
    }
    .file-item .file-link { flex: 1; min-width: 0; }
    .file-item form {
      margin: 0;
      display: flex;
      align-items: stretch;
    }
    .btn-delete {
      min-height: var(--tap-min);
      padding: 0 0.75rem;
      font-size: 0.875rem;
      font-weight: 600;
      font-family: inherit;
      color: #ffcdd2;
      background: rgba(229, 57, 53, 0.18);
      border: 1px solid rgba(229, 57, 53, 0.45);
      border-radius: calc(var(--radius) - 4px);
      cursor: pointer;
      white-space: nowrap;
    }
    .btn-delete:hover {
      background: rgba(229, 57, 53, 0.32);
      border-color: rgba(229, 57, 53, 0.65);
    }
    .btn-delete:active { transform: scale(0.98); }
    .empty {
      margin: 0;
      padding: 1rem;
      text-align: center;
      font-size: 0.9rem;
      color: var(--text-muted);
      border: 1px dashed var(--border);
      border-radius: calc(var(--radius) - 4px);
    }
    .banner {
      margin: 0 0 0.75rem;
      padding: 0.65rem 0.85rem;
      border-radius: calc(var(--radius) - 4px);
      font-size: 0.9rem;
      line-height: 1.4;
    }
    .banner--ok {
      color: #c8e6c9;
      background: rgba(76, 175, 80, 0.15);
      border: 1px solid rgba(76, 175, 80, 0.45);
    }
    .banner--err {
      color: #ffcdd2;
      background: rgba(229, 57, 53, 0.15);
      border: 1px solid rgba(229, 57, 53, 0.45);
    }
    .view-switch {
      display: flex;
      gap: 0;
      margin-bottom: 0.75rem;
      border: 1px solid var(--border);
      border-radius: calc(var(--radius) - 4px);
      overflow: hidden;
    }
    .view-switch a {
      flex: 1;
      text-align: center;
      padding: 0.55rem 0.65rem;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
      text-decoration: none;
      background: var(--bg-input);
    }
    .view-switch a.is-active {
      color: var(--text);
      background: var(--bg-elevated);
      box-shadow: inset 0 -2px 0 0 var(--accent);
    }
    .view-switch a + a { border-left: 1px solid var(--border); }
    ul.file-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
    }
    ul.thumb-grid {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.75rem;
    }
    @media (min-width: 400px) {
      ul.thumb-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    .thumb-card {
      border: 1px solid var(--border);
      border-radius: calc(var(--radius) - 4px);
      overflow: hidden;
      background: var(--bg-input);
      display: flex;
      flex-direction: column;
      min-width: 0;
    }
    .thumb-preview {
      display: block;
      aspect-ratio: 1;
      background: #0a0e14;
      position: relative;
    }
    .thumb-preview img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .thumb-placeholder {
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.06em;
    }
    .thumb-meta {
      padding: 0.45rem 0.5rem 0.5rem;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      flex: 1;
    }
    .thumb-name {
      font-size: 0.72rem;
      color: var(--accent);
      word-break: break-word;
      text-decoration: none;
      line-height: 1.25;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .thumb-name:hover { color: var(--accent-hover); }
    .thumb-actions { margin-top: auto; }
    .thumb-actions form { margin: 0; display: block; }
    .thumb-actions .btn-delete {
      min-height: 40px;
      width: 100%;
      font-size: 0.8rem;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Upload files</h1>
      <p class="lede">txt, pdf, png, jpg, jpeg, gif — multiple files allowed</p>
    </header>
    <section class="card" aria-labelledby="upload-heading">
      {% if upload_message %}
      <p class="banner banner--{{ upload_status }}" role="status">{{ upload_message }}</p>
      {% endif %}
      <h2 id="upload-heading">Add files</h2>
      <form class="upload-form" method="post" enctype="multipart/form-data" action="/upload">
        <input type="hidden" name="view" value="{{ view_mode }}">
        <div class="file-row">
          <input type="file" name="file" id="file" multiple required>
          <input type="submit" value="Upload">
        </div>
        <p class="hint">Choose one or more files (e.g. Shift/Ctrl/Cmd+click). Stored on the server.</p>
      </form>
    </section>
    <section aria-labelledby="files-heading">
      <h2 id="files-heading">Uploaded files</h2>
      <div class="view-switch" role="group" aria-label="How to show files">
        <a href="{{ url_for('upload_form', view='thumb') }}" class="{% if view_mode == 'thumb' %}is-active{% endif %}">Thumbnails</a>
        <a href="{{ url_for('upload_form', view='list') }}" class="{% if view_mode == 'list' %}is-active{% endif %}">List</a>
      </div>
      {% if files %}
        {% if view_mode == 'list' %}
      <ul class="file-list">
        {% for filename in files %}
        <li class="file-item">
          <a class="file-link" href="{{ url_for('file_detail', filename=filename, view=view_mode) }}">{{ filename }}</a>
          <form method="post" action="/delete" onsubmit="return confirm('Delete this file?');">
            <input type="hidden" name="filename" value="{{ filename|e }}">
            <input type="hidden" name="view" value="{{ view_mode }}">
            <button type="submit" class="btn-delete" aria-label="Delete {{ filename|e }}">Delete</button>
          </form>
        </li>
        {% endfor %}
      </ul>
        {% else %}
      <ul class="thumb-grid">
        {% for filename in files %}
        {% set parts = filename.rsplit('.', 1) %}
        {% set ext = parts[1].lower() if parts|length == 2 else '' %}
        {% set is_img = ext in ['png', 'jpg', 'jpeg', 'gif'] %}
        <li>
          <div class="thumb-card">
            <a class="thumb-preview" href="{{ url_for('file_detail', filename=filename, view=view_mode) }}" title="Details for {{ filename|e }}">
              {% if is_img %}
              <img src="{{ url_for('uploaded_file', filename=filename) }}" alt="" loading="lazy" width="200" height="200">
              {% else %}
              <span class="thumb-placeholder"><span>{{ ext|upper if ext else 'FILE' }}</span></span>
              {% endif %}
            </a>
            <div class="thumb-meta">
              <a class="thumb-name" href="{{ url_for('file_detail', filename=filename, view=view_mode) }}">{{ filename }}</a>
              <div class="thumb-actions">
                <form method="post" action="/delete" onsubmit="return confirm('Delete this file?');">
                  <input type="hidden" name="filename" value="{{ filename|e }}">
                  <input type="hidden" name="view" value="{{ view_mode }}">
                  <button type="submit" class="btn-delete" aria-label="Delete {{ filename|e }}">Delete</button>
                </form>
              </div>
            </div>
          </div>
        </li>
        {% endfor %}
      </ul>
        {% endif %}
      {% else %}
      <p class="empty">No files yet — upload something above.</p>
      {% endif %}
    </section>
  </div>
</body>
</html>
"""


DETAIL_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0f1419">
  <title>{{ filename|e }} — File</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1419;
      --bg-elevated: #1a2332;
      --border: #2d3a4d;
      --text: #e7ecf3;
      --text-muted: #8b9aad;
      --accent: #5b9fd4;
      --accent-hover: #7eb6e8;
      --radius: 12px;
      --tap-min: 44px;
      --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      font-family: var(--font);
      font-size: 1rem;
      line-height: 1.45;
      color: var(--text);
      background: var(--bg);
      -webkit-font-smoothing: antialiased;
      padding:
        max(1rem, env(safe-area-inset-top))
        max(1rem, env(safe-area-inset-right))
        max(1.25rem, env(safe-area-inset-bottom))
        max(1rem, env(safe-area-inset-left));
    }
    .wrap { width: 100%; max-width: 36rem; margin: 0 auto; }
    .back {
      display: inline-block;
      margin-bottom: 1rem;
      font-size: 0.9rem;
      color: var(--accent);
      text-decoration: none;
    }
    .back:hover { color: var(--accent-hover); }
    h1 {
      font-size: clamp(1.1rem, 3.5vw, 1.35rem);
      font-weight: 600;
      margin: 0 0 1rem;
      word-break: break-word;
    }
    .hero-img {
      display: block;
      width: 100%;
      max-height: min(70dvh, 520px);
      height: auto;
      object-fit: contain;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: #0a0e14;
      margin-bottom: 1rem;
    }
    .no-preview {
      padding: 2rem 1rem;
      text-align: center;
      color: var(--text-muted);
      border: 1px dashed var(--border);
      border-radius: var(--radius);
      margin-bottom: 1rem;
      font-size: 0.95rem;
    }
    .card {
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem;
      margin-bottom: 1rem;
    }
    .card h2 {
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin: 0 0 0.5rem;
    }
    dl.meta {
      margin: 0;
      display: grid;
      grid-template-columns: minmax(6rem, 38%) 1fr;
      gap: 0.35rem 0.75rem;
      font-size: 0.85rem;
    }
    dl.meta dt {
      margin: 0;
      color: var(--text-muted);
      font-weight: 500;
    }
    dl.meta dd {
      margin: 0;
      word-break: break-word;
    }
    .banner {
      margin: 0 0 0.75rem;
      padding: 0.65rem 0.85rem;
      border-radius: calc(var(--radius) - 4px);
      font-size: 0.88rem;
      color: #8b9aad;
      background: rgba(139, 154, 173, 0.12);
      border: 1px solid rgba(139, 154, 173, 0.35);
    }
    .actions {
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
    }
    @media (min-width: 400px) {
      .actions { flex-direction: row; flex-wrap: wrap; align-items: stretch; }
    }
    .btn {
      min-height: var(--tap-min);
      padding: 0 1rem;
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      border-radius: calc(var(--radius) - 4px);
      cursor: pointer;
      text-align: center;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: none;
      flex: 1;
      min-width: 8rem;
    }
    .btn-download {
      color: var(--bg);
      background: var(--accent);
    }
    .btn-download:hover { background: var(--accent-hover); }
    .btn-delete {
      color: #ffcdd2;
      background: rgba(229, 57, 53, 0.18);
      border: 1px solid rgba(229, 57, 53, 0.45);
    }
    .btn-delete:hover {
      background: rgba(229, 57, 53, 0.32);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <a class="back" href="{{ url_for('upload_form', view=view_mode) }}">← All files</a>
    <h1>{{ filename|e }}</h1>

    {% if is_image %}
    <img class="hero-img" src="{{ download_url }}" alt="{{ filename|e }}" width="800" height="600">
    {% else %}
    <div class="no-preview">Preview is not available for this file type. Use Download to open it.</div>
    {% endif %}

    {% if meta_note %}
    <p class="banner" role="note">{{ meta_note }}</p>
    {% endif %}

    {% if lmstudio_meta %}
    <div class="card">
      <h2>Photo metadata</h2>
      <dl class="meta">
        {% if lmstudio_meta.get('people') %}
        <dt>People</dt>
        <dd>{{ lmstudio_meta.people | join(', ') | e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('event') %}
        <dt>Event</dt>
        <dd>{{ lmstudio_meta.event|e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('date') %}
        <dt>Date</dt>
        <dd>{{ lmstudio_meta.date|e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('location') %}
        <dt>Location</dt>
        <dd>{{ lmstudio_meta.location|e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('has_people') is not none and lmstudio_meta.get('people_count') is not none %}
        <dt>People count</dt>
        <dd>{{ lmstudio_meta.people_count }} ({{ lmstudio_meta.get('distinct_people', '?') }} distinct)</dd>
        {% endif %}
        {% if lmstudio_meta.notes %}
        <dt>Notes</dt>
        <dd>{{ lmstudio_meta.notes|e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('last_updated') %}
        <dt>Last updated</dt>
        <dd>{{ lmstudio_meta.last_updated|e }}</dd>
        {% elif lmstudio_meta.get('generated_at') %}
        <dt>Analyzed</dt>
        <dd>{{ lmstudio_meta.generated_at|e }}</dd>
        {% endif %}
        {% if lmstudio_meta.get('model') %}
        <dt>Model</dt>
        <dd><code>{{ lmstudio_meta.model|e }}</code></dd>
        {% endif %}
      </dl>
    </div>
    {% endif %}

    <div class="card">
      <h2>File</h2>
      <dl class="meta">
        {% for label, value in meta_general %}
        <dt>{{ label|e }}</dt><dd>{{ value|e }}</dd>
        {% endfor %}
      </dl>
    </div>

    {% if meta_image %}
    <div class="card">
      <h2>Image</h2>
      <dl class="meta">
        {% for label, value in meta_image %}
        <dt>{{ label|e }}</dt><dd>{{ value|e }}</dd>
        {% endfor %}
      </dl>
    </div>
    {% endif %}

    {% if meta_exif %}
    <div class="card">
      <h2>EXIF &amp; tags</h2>
      <dl class="meta">
        {% for label, value in meta_exif %}
        <dt>{{ label|e }}</dt><dd>{{ value|e }}</dd>
        {% endfor %}
      </dl>
    </div>
    {% endif %}

    <div class="actions">
      <a class="btn btn-download" href="{{ download_url }}" download>Download</a>
      <form method="post" action="/delete" style="flex:1;min-width:8rem;margin:0;display:flex;"
            onsubmit="return confirm('Delete this file permanently?');">
        <input type="hidden" name="filename" value="{{ filename|e }}">
        <input type="hidden" name="view" value="{{ view_mode }}">
        <button type="submit" class="btn btn-delete" style="width:100%">Delete</button>
      </form>
    </div>
  </div>
</body>
</html>
"""



def _render_upload_page(
    upload_message=None, upload_status="ok", view_mode="thumb"
):
    return render_template_string(
        HTML_TEMPLATE,
        files=list_upload_folder(),
        upload_message=upload_message,
        upload_status=upload_status,
        view_mode=view_mode,
    )


@app.route("/", methods=["GET"])
def upload_form():
    vm = _normalize_view_mode(request.args.get("view"))
    return _render_upload_page(view_mode=vm)


@app.route("/upload", methods=["POST"])
def upload_file():
    vm = _normalize_view_mode(request.form.get("view"))
    selected = [f for f in request.files.getlist("file") if f and f.filename]
    if not selected:
        return _render_upload_page("No file selected.", "err", vm), 400

    saved = []
    rejected = []

    for file in selected:
        name = file.filename
        if not allowed_file(name):
            rejected.append((name, "type not allowed"))
            continue
        safe_name = secure_filename(name)
        if not safe_name:
            rejected.append((name, "invalid filename"))
            continue
        dest = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
        file.save(dest)
        saved.append(safe_name)

    if not saved:
        detail = "; ".join(f"{n} ({reason})" for n, reason in rejected)
        return _render_upload_page(f"Nothing uploaded. {detail}", "err", vm), 400

    parts = [f"Saved {len(saved)} file(s): " + ", ".join(saved)]
    if rejected:
        parts.append(
            "Skipped: " + "; ".join(f"{n} ({reason})" for n, reason in rejected)
        )
    return _render_upload_page(" ".join(parts), "ok", vm), 200


@app.route("/delete", methods=["POST"])
def delete_uploaded_file():
    vm = _normalize_view_mode(request.form.get("view"))
    raw = request.form.get("filename", "").strip()
    if not raw:
        return _render_upload_page("No file chosen to delete.", "err", vm), 400

    base = Path(app.config["UPLOAD_FOLDER"]).resolve()
    target = (base / raw).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return _render_upload_page("Invalid path.", "err", vm), 400

    if not target.is_file():
        return _render_upload_page(f"Not found: {raw}", "err", vm), 404

    try:
        target.unlink()
    except OSError as exc:
        return _render_upload_page(f"Could not delete file: {exc}", "err", vm), 500

    sidecar = target.parent / (target.name + ".meta.json")
    if sidecar.is_file():
        try:
            sidecar.unlink()
        except OSError:
            pass

    return _render_upload_page(f"Deleted `{target.name}`.", "ok", vm), 200


@app.route("/files")
def list_files():
    vm = _normalize_view_mode(request.args.get("view"))
    return _render_upload_page(view_mode=vm)


@app.route("/file/<path:filename>")
def file_detail(filename):
    target = _resolved_upload_target(filename)
    if target is None:
        abort(404)
    vm = _normalize_view_mode(request.args.get("view"))
    bundle = build_metadata_rows(target)
    lm_raw = load_lmstudio_sidecar(target)
    return render_template_string(
        DETAIL_TEMPLATE,
        filename=filename,
        view_mode=vm,
        download_url=url_for("uploaded_file", filename=filename),
        meta_general=bundle["general"],
        meta_image=bundle["image"],
        meta_exif=bundle["exif"],
        is_image=bundle["is_image"],
        meta_note=bundle["note"],
        lmstudio_meta=lm_raw,
    )


@app.route("/health")
def health():
    upload_path = app.config["UPLOAD_FOLDER"]
    exists = os.path.isdir(upload_path)
    writable = exists and os.access(upload_path, os.W_OK)
    uptime = time.monotonic() - _started_monotonic
    try:
        count = len(list_upload_folder())
    except OSError:
        count = None
    ok = exists and writable and count is not None
    payload = {
        "status": "healthy" if ok else "degraded",
        "uptime_seconds": round(uptime, 3),
        "upload_folder": {
            "path": upload_path,
            "exists": exists,
            "writable": writable,
        },
        "uploaded_file_count": count,
    }
    code = 200 if ok else 503
    return jsonify(payload), code


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    target = _resolved_upload_target(filename)
    if target is None:
        abort(404)
    base = Path(app.config["UPLOAD_FOLDER"]).resolve()
    rel = target.relative_to(base)
    return send_from_directory(str(base), rel.as_posix())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

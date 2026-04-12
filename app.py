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
        if not f.startswith(".") and os.path.isfile(os.path.join(UPLOAD_FOLDER, f))
    )


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
    form { margin: 0; display: flex; flex-direction: column; gap: 0.75rem; }
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
    ul {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
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
    .empty {
      margin: 0;
      padding: 1rem;
      text-align: center;
      font-size: 0.9rem;
      color: var(--text-muted);
      border: 1px dashed var(--border);
      border-radius: calc(var(--radius) - 4px);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Upload a file</h1>
      <p class="lede">txt, pdf, png, jpg, jpeg, gif</p>
    </header>
    <section class="card" aria-labelledby="upload-heading">
      <h2 id="upload-heading">Add file</h2>
      <form method="post" enctype="multipart/form-data" action="/upload">
        <div class="file-row">
          <input type="file" name="file" id="file" required>
          <input type="submit" value="Upload">
        </div>
        <p class="hint">Files are stored in the server upload folder.</p>
      </form>
    </section>
    <section aria-labelledby="files-heading">
      <h2 id="files-heading">Uploaded files</h2>
      {% if files %}
      <ul>
        {% for filename in files %}
        <li>
          <a class="file-link" href="{{ url_for('uploaded_file', filename=filename) }}">{{ filename }}</a>
        </li>
        {% endfor %}
      </ul>
      {% else %}
      <p class="empty">No files yet — upload something above.</p>
      {% endif %}
    </section>
  </div>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def upload_form():
    return render_template_string(HTML_TEMPLATE, files=list_upload_folder())


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return "No file part in the request", 400

    file = request.files["file"]

    if file.filename == "":
        return "No selected file", 400

    if file and allowed_file(file.filename):
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return "Invalid filename", 400
        dest = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
        file.save(dest)
        return (
            f"File {safe_name} successfully uploaded to {app.config['UPLOAD_FOLDER']}"
        )

    return "File type not allowed", 400


@app.route("/files")
def list_files():
    return render_template_string(HTML_TEMPLATE, files=list_upload_folder())


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
    base = Path(app.config["UPLOAD_FOLDER"]).resolve()
    target = (base / filename).resolve()
    try:
        rel = target.relative_to(base)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    return send_from_directory(str(base), str(rel))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

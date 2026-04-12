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
<title>File Uploader</title>
<h1>Upload a File</h1>
<form method=post enctype=multipart/form-data action="/upload">
  <input type=file name=file>
  <input type=submit value=Upload>
</form>
<h2>Uploaded Files:</h2>
<ul>
{% for filename in files %}
    <li><a href="{{ url_for('uploaded_file', filename=filename) }}">{{ filename }}</a></li>
{% endfor %}
</ul>
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

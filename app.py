import threading
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from services import jobs, sidecar, lmstudio

# --- Configuration ---
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
PEOPLE_FOLDER = os.path.join(os.getcwd(), "people")
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

_started_monotonic = time.monotonic()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def list_people():
    """Return list of person dicts from people/<uuid>/person.json files."""
    if not os.path.isdir(PEOPLE_FOLDER):
        return []
    people = []
    for uuid_dir in os.listdir(PEOPLE_FOLDER):
        person_file = os.path.join(PEOPLE_FOLDER, uuid_dir, "person.json")
        if not os.path.isfile(person_file):
            continue
        try:
            with open(person_file, "r", encoding="utf-8") as f:
                person = json.load(f)
            person["id"] = uuid_dir
            people.append(person)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return sorted(people, key=lambda p: p.get("name", "").lower())


def count_tagged_photos(person_name: str) -> int:
    """Count photos in uploads tagged with the given person name."""
    results = sidecar.search(Path(UPLOAD_FOLDER), person_name)
    return len(results)


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
    return sidecar.read(upload_path)


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


def _render_upload_page(
    upload_message=None, upload_status="ok", view_mode="thumb"
):
    return render_template(
        "library.html",
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

    sidecar.delete(target)

    return _render_upload_page(f"Deleted `{target.name}`.", "ok", vm), 200


@app.route("/files")
def list_files():
    vm = _normalize_view_mode(request.args.get("view"))
    return _render_upload_page(view_mode=vm)


@app.route("/people")
def people_page():
    people = list_people()
    for person in people:
        person["tagged_count"] = count_tagged_photos(person.get("name", ""))
    return render_template("people.html", people=people)


@app.route("/people/new", methods=["GET", "POST"])
def new_person():
    if request.method == "GET":
        return render_template("person_form.html", person=None, error=None)

    name = request.form.get("name", "").strip()
    if not name:
        return render_template("person_form.html", person=None, error="Name is required."), 400

    photos = [f for f in request.files.getlist("photos") if f and f.filename]
    if not photos:
        return render_template("person_form.html", person=None, error="At least one reference photo is required."), 400

    person_id = str(uuid.uuid4())
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    os.makedirs(person_dir, exist_ok=True)

    saved_photos = []
    for photo in photos:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
        safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        photo.save(os.path.join(person_dir, safe_name))
        saved_photos.append(safe_name)

    person_data = {"id": person_id, "name": name, "reference_photos": saved_photos}
    with open(os.path.join(person_dir, "person.json"), "w", encoding="utf-8") as f:
        json.dump(person_data, f, indent=2)

    return redirect(url_for("people_page"))


@app.route("/people/<person_id>/edit", methods=["GET", "POST"])
def edit_person(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")

    if not os.path.isfile(person_file):
        abort(404)

    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)
    person["id"] = person_id

    if request.method == "GET":
        return render_template("person_form.html", person=person, error=None)

    name = request.form.get("name", "").strip()
    if not name:
        return render_template("person_form.html", person=person, error="Name is required."), 400

    photos = [f for f in request.files.getlist("photos") if f and f.filename]
    new_photos = []
    for photo in photos:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
        safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        photo.save(os.path.join(person_dir, safe_name))
        new_photos.append(safe_name)

    person["name"] = name
    person["reference_photos"] = person.get("reference_photos", []) + new_photos

    with open(person_file, "w", encoding="utf-8") as f:
        json.dump(person, f, indent=2)

    return redirect(url_for("people_page"))


@app.route("/people/<person_id>/delete", methods=["POST"])
def delete_person(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    if not os.path.isdir(person_dir):
        abort(404)

    shutil.rmtree(person_dir)

    return redirect(url_for("people_page"))


@app.route("/people/<person_id>")
def person_detail(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")

    if not os.path.isfile(person_file):
        abort(404)

    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)
    person["id"] = person_id

    tagged_results = sidecar.search(Path(UPLOAD_FOLDER), person.get("name", ""))
    tagged_photos = [(path, meta) for path, meta in tagged_results]

    return render_template(
        "person_detail.html",
        person=person,
        tagged_photos=tagged_photos,
        tagged_count=len(tagged_photos),
    )


@app.route("/people/<person_id>/add-photo", methods=["POST"])
def add_person_photo(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")

    if not os.path.isfile(person_file):
        abort(404)

    photo = request.files.get("photo")
    if not photo or not photo.filename:
        abort(400)

    ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
    safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    photo.save(os.path.join(person_dir, safe_name))

    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)

    person["reference_photos"] = person.get("reference_photos", []) + [safe_name]

    with open(person_file, "w", encoding="utf-8") as f:
        json.dump(person, f, indent=2)

    return redirect(url_for("person_detail", person_id=person_id))


@app.route("/people/ref/<person_id>/<filename>")
def person_ref_photo(person_id, filename):
    safe_filename = secure_filename(filename)
    return send_from_directory(os.path.join(PEOPLE_FOLDER, person_id), safe_filename)


@app.route("/file/<path:filename>")
def file_detail(filename):
    target = _resolved_upload_target(filename)
    if target is None:
        abort(404)
    vm = _normalize_view_mode(request.args.get("view"))
    bundle = build_metadata_rows(target)
    lm_raw = load_lmstudio_sidecar(target)
    return render_template(
        "detail.html",
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


@app.route("/api/jobs/<job_id>")
def job_stream(job_id):
    job = jobs.get_job(job_id)
    if job is None:
        abort(404)

    def generate():
        for update in jobs.stream_progress(job_id):
            data = {"progress": update.get("progress", 0), "message": update.get("message", "")}
            if update.get("result") is not None:
                data["result"] = update["result"]
            if update.get("error"):
                data["error"] = update["error"]
            yield f"data: {json.dumps(data)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/jobs/<job_id>/result")
def job_result(job_id):
    job = jobs.get_job(job_id)
    if job is None:
        abort(404)
    data = job.to_dict()
    if data["status"] == "failed":
        return jsonify({"status": "failed", "error": data.get("error")}), 200
    return jsonify({"status": data["status"], "result": data.get("result")}), 200


@app.route("/api/lmstudio/status")
def lmstudio_status():
    base = os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "")
    server = "up" if lmstudio.server_is_up(base) else "down"
    model_state = "unloaded"
    if server == "up" and model:
        model_state = "loaded" if lmstudio.model_is_loaded(base, model) else "unloaded"
    return jsonify({"server": server, "model": model_state})


def _start_lmstudio_background():
    base = os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "")
    if model:
        t = threading.Thread(target=lambda: lmstudio.ensure_ready(base, model), daemon=True)
        t.start()


if __name__ == "__main__":
    _start_lmstudio_background()
    app.run(host="0.0.0.0", port=8080)

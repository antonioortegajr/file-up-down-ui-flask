import datetime
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

from services import confirmations, jobs, sidecar, lmstudio

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
_lms_start_error = None


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


def list_files_by_date(filter_mode: str = "all") -> list[tuple[str, datetime.datetime | None]]:
    """Return list of (filename, date) sorted by date descending. None for unknown date."""
    files = list_upload_folder()
    if filter_mode == "favorites":
        favorited_set = set(list_favorited_files())
        files = [f for f in files if f in favorited_set]

    result = []
    for filename in files:
        path = Path(UPLOAD_FOLDER) / filename
        dt = get_photo_date(path)
        result.append((filename, dt))

    result.sort(key=lambda x: x[1] if x[1] else datetime.datetime.min, reverse=True)
    return result


def list_favorited_files():
    """Return list of files where sidecar metadata has favorited: true."""
    if not os.path.isdir(UPLOAD_FOLDER):
        return []
    favorited = []
    for f in os.listdir(UPLOAD_FOLDER):
        if f.startswith("."):
            continue
        if f.endswith(".meta.json") or f == "_set_unique.meta.json":
            continue
        path = Path(UPLOAD_FOLDER) / f
        if not path.is_file():
            continue
        meta = sidecar.read(path)
        if meta and meta.get("favorited"):
            favorited.append(f)
    return sorted(favorited)


def is_favorited(filename: str) -> bool:
    """Check if a file is favorited via its sidecar metadata."""
    path = Path(UPLOAD_FOLDER) / filename
    if not path.is_file():
        return False
    meta = sidecar.read(path)
    return bool(meta and meta.get("favorited"))


def get_photo_date(path: Path) -> datetime.datetime | None:
    """Extract DateTimeOriginal from EXIF, or fall back to file mtime."""
    ext = path.suffix.lower().lstrip(".")
    if ext not in IMAGE_EXTENSIONS:
        return None

    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if exif is not None:
                from PIL.ExifTags import TAGS
                for tag_id, val in exif.items():
                    if TAGS.get(tag_id) == "DateTimeOriginal":
                        try:
                            return datetime.datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass

    try:
        mtime = path.stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime)
    except (OSError, ValueError):
        return None


def load_lmstudio_sidecar(upload_path: Path):
    """Read `imagename.meta.json` written by analyze_uploads_people_lmstudio.py, or empty dict."""
    return sidecar.read(upload_path) or {}


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
    if v == "timeline":
        return "timeline"
    return "thumb"


def _render_upload_page(
    upload_message=None,
    upload_status="ok",
    view_mode="thumb",
    describe_job_id=None,
    filter_mode="all",
):
    files = list_favorited_files() if filter_mode == "favorites" else list_upload_folder()
    favorited_set = {f for f in list_upload_folder() if is_favorited(f)}

    timeline_groups = None
    if view_mode == "timeline":
        timeline_groups = _group_files_by_date(filter_mode)

    return render_template(
        "library.html",
        files=files,
        upload_message=upload_message,
        upload_status=upload_status,
        view_mode=view_mode,
        describe_job_id=describe_job_id,
        filter_mode=filter_mode,
        favorited_set=favorited_set,
        timeline_groups=timeline_groups,
    )


def _group_files_by_date(filter_mode: str) -> list[tuple[str, list[tuple[str, datetime.datetime | None]]]]:
    """Group files by Year → Month. Returns list of (label, [(filename, date), ...])."""
    files_with_dates = list_files_by_date(filter_mode)

    groups = {}
    unknown = []
    for filename, dt in files_with_dates:
        if dt is None:
            unknown.append((filename, dt))
            continue
        year = dt.year
        month = dt.month
        key = (year, month)
        if key not in groups:
            groups[key] = []
        groups[key].append((filename, dt))

    for key in groups:
        groups[key].sort(key=lambda x: x[1], reverse=True)

    sorted_keys = sorted(groups.keys(), reverse=True)

    result = []
    for key in sorted_keys:
        year, month = key
        month_name = datetime.date(year, month, 1).strftime("%B %Y")
        result.append((month_name, groups[key]))

    if unknown:
        result.append(("Unknown date", unknown))

    return result


@app.route("/", methods=["GET"])
def upload_form():
    if not list_people():
        return redirect(url_for("walkthrough", step=1))
    vm = _normalize_view_mode(request.args.get("view"))
    filter_mode = request.args.get("filter", "all")
    if filter_mode not in ("all", "favorites"):
        filter_mode = "all"
    return _render_upload_page(view_mode=vm, filter_mode=filter_mode)


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

    describe_job_id = None
    images_needing_desc = []
    for fname in saved:
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext not in IMAGE_EXTENSIONS:
            continue
        fpath = Path(app.config["UPLOAD_FOLDER"]) / fname
        if sidecar.read_desc_cache(fpath) is None:
            images_needing_desc.append(str(fpath))

    if images_needing_desc:
        base_url = lmstudio.LMSTUDIO_BASE
        model = os.environ.get("LMSTUDIO_MODEL", "")
        api_key = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")
        if model:
            lmstudio.ensure_ready(base_url, model)
            describe_job_id = jobs.queue_job(
                lmstudio.describe_new_photos,
                images_needing_desc,
                base_url,
                model,
                api_key,
            )

    msg = " ".join(parts)
    return _render_upload_page(msg, "ok", vm, describe_job_id), 200


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


@app.route("/api/photos/<path:filename>/favorite", methods=["POST"])
def toggle_favorite(filename: str):
    """Toggle favorite status for a photo. Returns current favorited state."""
    target = _resolved_upload_target(filename)
    if target is None:
        return jsonify({"error": "File not found"}), 404

    current = sidecar.read(target) or {}
    favorited = not current.get("favorited", False)
    sidecar.merge(target, {"favorited": favorited})
    return jsonify({"favorited": favorited})


@app.route("/files")
def list_files():
    vm = _normalize_view_mode(request.args.get("view"))
    filter_mode = request.args.get("filter", "all")
    if filter_mode not in ("all", "favorites"):
        filter_mode = "all"
    return _render_upload_page(view_mode=vm, filter_mode=filter_mode)


@app.route("/people")
def people_page():
    if request.method == "GET" and not list_people():
        return redirect(url_for("walkthrough", step=1))
    people = list_people()
    for person in people:
        person["tagged_count"] = count_tagged_photos(person.get("name", ""))
    return render_template("people.html", people=people)


@app.route("/walkthrough", methods=["GET", "POST"])
def walkthrough():
    step = int(request.args.get("step", 1))
    person_id = request.args.get("person_id")
    person_name = request.args.get("person_name", "")

    if step == 1:
        return render_template("walkthrough.html", step=1)

    if step == 2:
        library_files = list_upload_folder()
        image_exts = IMAGE_EXTENSIONS
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                return render_template("walkthrough.html", step=2, error="Name is required.", library_photos=library_files, image_exts=image_exts)

            uploaded_photos = [f for f in request.files.getlist("photos") if f and f.filename]
            library_photo_selections = request.form.get("library_photos", "").strip()

            selected_library = []
            if library_photo_selections:
                valid_lib = set(library_files)
                for fn in library_photo_selections.split(","):
                    fn = fn.strip()
                    if fn and fn in valid_lib:
                        selected_library.append(fn)

            if not uploaded_photos and not selected_library:
                return render_template("walkthrough.html", step=2, error="At least one reference photo is required.", library_photos=library_files, image_exts=image_exts)

            pid = str(uuid.uuid4())
            person_dir = os.path.join(PEOPLE_FOLDER, pid)
            os.makedirs(person_dir, exist_ok=True)

            saved_photos = []

            for fn in selected_library:
                src = os.path.join(UPLOAD_FOLDER, fn)
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else "jpg"
                dest_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
                dest = os.path.join(person_dir, dest_name)
                shutil.copy(src, dest)
                saved_photos.append(dest_name)

            for photo in uploaded_photos:
                ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
                safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
                photo.save(os.path.join(person_dir, safe_name))
                saved_photos.append(safe_name)

            person_data = {"id": pid, "name": name, "reference_photos": saved_photos}
            with open(os.path.join(person_dir, "person.json"), "w", encoding="utf-8") as f:
                json.dump(person_data, f, indent=2)

            return redirect(url_for("walkthrough", step=3, person_id=pid, person_name=name))

        return render_template("walkthrough.html", step=2, library_photos=library_files, image_exts=image_exts)

    if step == 3:
        if not person_id:
            return redirect(url_for("walkthrough", step=1))
        return render_template("walkthrough.html", step=3, person_id=person_id, person_name=person_name)

    if step == 5:
        if not person_id:
            return redirect(url_for("upload_form"))
        tagged_count = count_tagged_photos(person_name)
        return render_template(
            "walkthrough.html",
            step=5,
            person_id=person_id,
            person_name=person_name,
            tagged_count=tagged_count,
        )

    return redirect(url_for("walkthrough", step=1))


@app.route("/people/new", methods=["GET", "POST"])
def new_person():
    library_files = list_upload_folder()
    image_exts = IMAGE_EXTENSIONS

    if request.method == "GET":
        return render_template("person_form.html", person=None, error=None, library_photos=library_files, image_exts=image_exts)

    name = request.form.get("name", "").strip()
    if not name:
        return render_template("person_form.html", person=None, error="Name is required.", library_photos=library_files, image_exts=image_exts), 400

    uploaded_photos = [f for f in request.files.getlist("photos") if f and f.filename]
    library_photo_selections = request.form.get("library_photos", "").strip()

    selected_library = []
    if library_photo_selections:
        valid_lib = set(library_files)
        for fn in library_photo_selections.split(","):
            fn = fn.strip()
            if fn and fn in valid_lib:
                selected_library.append(fn)

    if not uploaded_photos and not selected_library:
        return render_template("person_form.html", person=None, error="At least one reference photo is required.", library_photos=library_files, image_exts=image_exts), 400

    person_id = str(uuid.uuid4())
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    os.makedirs(person_dir, exist_ok=True)

    saved_photos = []

    for fn in selected_library:
        src = os.path.join(UPLOAD_FOLDER, fn)
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else "jpg"
        dest_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        dest = os.path.join(person_dir, dest_name)
        shutil.copy(src, dest)
        saved_photos.append(dest_name)

    for photo in uploaded_photos:
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

    library_files = list_upload_folder()
    image_exts = IMAGE_EXTENSIONS

    if request.method == "GET":
        return render_template("person_form.html", person=person, error=None, library_photos=library_files, image_exts=image_exts)

    name = request.form.get("name", "").strip()
    if not name:
        return render_template("person_form.html", person=person, error="Name is required.", library_photos=library_files, image_exts=image_exts), 400

    uploaded_photos = [f for f in request.files.getlist("photos") if f and f.filename]
    library_photo_selections = request.form.get("library_photos", "").strip()

    selected_library = []
    if library_photo_selections:
        valid_lib = set(library_files)
        for fn in library_photo_selections.split(","):
            fn = fn.strip()
            if fn and fn in valid_lib:
                selected_library.append(fn)

    new_photos = []

    for fn in selected_library:
        src = os.path.join(UPLOAD_FOLDER, fn)
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else "jpg"
        dest_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        dest = os.path.join(person_dir, dest_name)
        shutil.copy(src, dest)
        new_photos.append(dest_name)

    for photo in uploaded_photos:
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

    lms_connected = lmstudio.server_is_up()

    return render_template(
        "person_detail.html",
        person=person,
        tagged_photos=tagged_photos,
        tagged_count=len(tagged_photos),
        lms_connected=lms_connected,
    )


@app.route("/people/<person_id>/confirm", methods=["GET", "POST"])
def confirm_person(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")

    if not os.path.isfile(person_file):
        abort(404)

    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)
    person["id"] = person_id

    if request.method == "GET":
        job_id = request.args.get("job")
        walkthrough = request.args.get("walkthrough") == "1"
        if not job_id:
            abort(400)

        job = jobs.get_job(job_id)
        if not job:
            abort(404)

        job_data = job.to_dict()
        if job_data["status"] not in ("done", "failed"):
            return render_template(
                "confirm_grid.html",
                person=person,
                waiting=True,
                job_id=job_id,
                walkthrough=walkthrough,
            )

        if job_data["status"] == "failed":
            abort(500)

        result = job_data["result"]
        matches_filenames = result.get("matches", [])
        no_match_filenames = result.get("no_match", [])

        job_logs = job_data.get("log", [])
        file_to_answer = {}
        for entry in job_logs:
            if "..." in entry:
                parts = entry.split("...", 1)
                fname = parts[0].split("]", 1)[-1].strip()
                answer = parts[1].strip() if len(parts) > 1 else ""
                file_to_answer[fname] = answer

        matches = []
        for fname in matches_filenames:
            matches.append({
                "filename": fname,
                "reason": file_to_answer.get(fname, "Match"),
            })

        no_match = []
        for fname in no_match_filenames:
            no_match.append({
                "filename": fname,
                "reason": file_to_answer.get(fname, "No match"),
            })

        walkthrough = request.args.get("walkthrough") == "1"

        confidence_scores = {}
        confirm_data = confirmations.read_confirmations(person_id)
        for fname, photo in confirm_data.get("photos", {}).items():
            confidence_scores[fname] = {
                "confidence": photo.get("confidence", 0.0),
                "votes": photo.get("yes_votes", 0) + photo.get("no_votes", 0),
            }

        return render_template(
            "confirm_grid.html",
            person=person,
            waiting=False,
            matches=matches,
            no_match=no_match,
            walkthrough=walkthrough,
            confidence_scores=confidence_scores,
        )

    confirmed_filenames = request.form.getlist("confirmed")
    person_name = person.get("name", "")
    walkthrough = request.form.get("walkthrough") == "1"

    for fname in confirmed_filenames:
        image_path = Path(UPLOAD_FOLDER) / fname
        if image_path.is_file():
            current = sidecar.read(image_path) or {}
            people_list = current.get("people", [])
            if person_name not in people_list:
                people_list.append(person_name)
            sidecar.merge(image_path, {"people": people_list})

    if walkthrough:
        return redirect(url_for("walkthrough", step=5, person_id=person_id, person_name=person_name))

    return redirect(url_for("person_detail", person_id=person_id))


def _get_person_or_404(person_id: str) -> dict:
    """Resolve person_id to person dict, abort 404 if not found."""
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")
    if not os.path.isfile(person_file):
        abort(404)
    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)
    person["id"] = person_id
    return person


def _discover_photos_with_meta() -> list[str]:
    """Discover all filenames in uploads/ that have a .meta.json sidecar."""
    if not os.path.isdir(UPLOAD_FOLDER):
        return []
    return sorted(
        f[:-9]
        for f in os.listdir(UPLOAD_FOLDER)
        if f.endswith(".meta.json")
        and os.path.isfile(os.path.join(UPLOAD_FOLDER, f[:-9]))
    )


# ----------------------------------------------------------------------
# Confirmation session API routes
# ----------------------------------------------------------------------


@app.route("/api/people/<person_id>/confirm-session", methods=["POST"])
def create_confirm_session(person_id):
    """Create or resume a confirmation session for a person."""
    _get_person_or_404(person_id)

    existing_session = confirmations.find_active_session(person_id)
    if existing_session:
        session = confirmations.get_session(person_id, existing_session)
        queue = session.get("queue", []) if session else []
        return jsonify({
            "session_id": existing_session,
            "resumed": True,
            "total_in_queue": len(queue),
        })

    limit = request.json.get("limit", 200) if request.json else 200
    photo_queue = request.json.get("photo_queue") if request.json else None

    if photo_queue is None:
        photo_queue = _discover_photos_with_meta()

    photo_queue = photo_queue[:limit]

    if not photo_queue:
        return jsonify({
            "error": "No photos with metadata found",
        }), 400

    session_id = confirmations.create_session(person_id, photo_queue)
    return jsonify({
        "session_id": session_id,
        "resumed": False,
        "total_in_queue": len(photo_queue),
    })


@app.route("/api/people/<person_id>/confirm-session/<session_id>")
def get_confirm_session(person_id, session_id):
    """Get full session data with computed progress fields."""
    _get_person_or_404(person_id)

    session = confirmations.get_session(person_id, session_id)
    if session is None:
        return jsonify({"error": "Session not found"}), 404

    queue = session.get("queue", [])
    answered = session.get("answered", {})
    current_index = session.get("current_index", 0)

    answered_count = len(answered)
    remaining_count = len(queue) - current_index
    current_filename = queue[current_index] if current_index < len(queue) else None

    match_reason = None
    if current_filename:
        meta = sidecar.read(Path(UPLOAD_FOLDER) / current_filename)
        if meta:
            match_reason = meta.get("match_reason")

    return jsonify({
        "session_id": session["session_id"],
        "person_id": session["person_id"],
        "created_at": session["created_at"],
        "last_active": session["last_active"],
        "status": session["status"],
        "queue": queue,
        "answered": answered,
        "current_index": current_index,
        "current_filename": current_filename,
        "match_reason": match_reason,
        "progress": {
            "answered_count": answered_count,
            "remaining_count": remaining_count,
            "current_filename": current_filename,
        },
    })


@app.route("/api/people/<person_id>/confirm-session/<session_id>/vote", methods=["POST"])
def vote_in_session(person_id, session_id):
    """Record a vote and advance the session."""
    person = _get_person_or_404(person_id)

    session = confirmations.get_session(person_id, session_id)
    if session is None:
        return jsonify({"error": "Session not found"}), 404

    if not request.json:
        return jsonify({"error": "Request body required"}), 400

    filename = request.json.get("filename")
    vote = request.json.get("vote")

    if not filename or not vote:
        return jsonify({"error": "filename and vote are required"}), 400

    if vote not in ("yes", "no", "skip"):
        return jsonify({"error": "vote must be 'yes', 'no', or 'skip'"}), 400

    current_index = session.get("current_index", 0)
    queue = session.get("queue", [])

    if current_index >= len(queue) or queue[current_index] != filename:
        return jsonify({"error": "Filename does not match current session position"}), 400

    confirmed = False
    if vote == "yes":
        photo = confirmations.record_vote(person_id, session_id, filename, "yes")
        if photo.get("confirmed"):
            confirmed = True
            sidecar.merge_confirmation_score(
                str(Path(UPLOAD_FOLDER) / filename),
                person['name'],
                photo.get("confidence", 0.0),
                photo.get("yes_votes", 0) + photo.get("no_votes", 0),
            )
    elif vote == "no":
        confirmations.record_vote(person_id, session_id, filename, "no")

    session["answered"][filename] = vote
    confirmations.save_session(person_id, session_id, session)

    session = confirmations.advance_session(person_id, session_id)
    new_index = session.get("current_index", 0)
    session_done = new_index >= len(queue)
    next_filename = queue[new_index] if not session_done else None

    confirmations_data = confirmations.read_confirmations(person_id)
    photos = confirmations_data.get("photos", {})
    photo_data = photos.get(filename, {})
    yes_votes = photo_data.get("yes_votes", 0)
    no_votes = photo_data.get("no_votes", 0)
    confidence = photo_data.get("confidence", 0.0)

    return jsonify({
        "next_filename": next_filename,
        "session_done": session_done,
        "confidence": confidence,
        "yes_votes": yes_votes,
        "no_votes": no_votes,
        "confirmed": confirmed,
    })


@app.route("/people/<person_id>/interactive-confirm")
def interactive_confirm(person_id):
    """Render the interactive one-photo-at-a-time confirmation UI."""
    person = _get_person_or_404(person_id)
    session_id = request.args.get("session", "")
    return render_template(
        "interactive_confirm.html",
        person=person,
        session_id=session_id,
    )


@app.route("/people/<person_id>/relationship-report")
def relationship_report(person_id):
    """Render a confirmation report for a person."""
    person = _get_person_or_404(person_id)
    report = confirmations.build_report(person_id)
    return render_template(
        "relationship_report.html",
        person=person,
        report=report,
    )


@app.route("/api/people/<person_id>/relationship-map")
def relationship_map(person_id):
    """Return confirmation report for a person."""
    _get_person_or_404(person_id)

    report = confirmations.build_report(person_id)
    return jsonify(report)


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


def _find_match_reason(meta, query):
    """Return the first sidecar field that matched the query."""
    if not meta:
        return None
    query_lower = query.lower()
    keywords = query_lower.split()
    search_fields = ["people", "event", "date", "location", "notes", "subject", "source"]
    for field in search_fields:
        value = meta.get(field)
        if value is None:
            continue
        if isinstance(value, list):
            for v in value:
                if query_lower in str(v).lower():
                    return f"{field.title()}: {v}"
        elif isinstance(value, str):
            for kw in keywords:
                if kw in value.lower():
                    return f"{field.title()}: {value[:80]}{'...' if len(value) > 80 else ''}"
    return None


@app.route("/search")
def search_page():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "text")

    if mode == "person":
        return render_template("search.html", query=query, mode=mode, results=[], people=list_people())

    if not query:
        return render_template("search.html", query=None, mode=mode, results=[], people=list_people())

    raw_results = sidecar.search(Path(UPLOAD_FOLDER), query)
    results = []
    for path, meta in raw_results:
        match_reason = _find_match_reason(meta, query)
        results.append((path, meta, match_reason))

    return render_template("search.html", query=query, mode=mode, results=results, people=list_people())


@app.route("/file/<path:filename>")
def file_detail(filename):
    target = _resolved_upload_target(filename)
    if target is None:
        abort(404)
    vm = _normalize_view_mode(request.args.get("view"))
    bundle = build_metadata_rows(target)
    lm_raw = load_lmstudio_sidecar(target)
    favorited = is_favorited(filename)
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
        favorited=favorited,
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


@app.route("/api/people/<person_id>/find", methods=["POST"])
def find_person_in_library(person_id):
    person_dir = os.path.join(PEOPLE_FOLDER, person_id)
    person_file = os.path.join(person_dir, "person.json")

    if not os.path.isfile(person_file):
        abort(404)

    base_url = lmstudio.LMSTUDIO_BASE
    model = os.environ.get("LMSTUDIO_MODEL", "")
    api_key = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")

    job_id = jobs.queue_job(
        lmstudio.match_person_in_library,
        person_id,
        PEOPLE_FOLDER,
        UPLOAD_FOLDER,
        base_url,
        model,
        api_key,
    )

    return jsonify({"job_id": job_id}), 202


@app.route("/api/lmstudio/status")
def lmstudio_status():
    global _lms_start_error
    base = lmstudio.LMSTUDIO_BASE
    model = os.environ.get("LMSTUDIO_MODEL", "")
    server = "up" if lmstudio.server_is_up(base) else "down"
    model_state = "unloaded"
    if server == "up" and model:
        model_state = "loaded" if lmstudio.model_is_loaded(base, model) else "unloaded"
    available_models = []
    if server == "up":
        available_models = lmstudio.get_available_models(base)
    response = {
        "server": server,
        "model": model_state,
        "model_configured": model,
        "available_models": available_models,
    }
    if _lms_start_error:
        response["error"] = _lms_start_error
    return jsonify(response)


@app.route("/api/lmstudio/models")
def lmstudio_models():
    base = lmstudio.LMSTUDIO_BASE
    if not lmstudio.server_is_up(base):
        return jsonify({"models": []})
    models = lmstudio.get_available_models(base)
    return jsonify({"models": models})


@app.route("/api/lmstudio/start", methods=["POST"])
def lmstudio_start():
    global _lms_start_error
    base = lmstudio.LMSTUDIO_BASE
    model = os.environ.get("LMSTUDIO_MODEL", "")
    data = request.get_json() or {}
    if data.get("model"):
        model = data["model"]
    _lms_start_error = None
    def wrapped_ensure_ready():
        global _lms_start_error
        try:
            lmstudio.ensure_ready(base, model)
        except Exception as e:
            _lms_start_error = str(e)
    t = threading.Thread(target=wrapped_ensure_ready, daemon=True)
    t.start()
    return jsonify({"status": "starting", "model": model})


@app.route("/api/lmstudio/stop", methods=["POST"])
def lmstudio_stop():
    success = lmstudio.stop_server()
    if success:
        return jsonify({"status": "stopped"})
    return jsonify({"error": "Failed to stop server"}), 500


@app.route("/api/lmstudio/set-model", methods=["POST"])
def lmstudio_set_model():
    data = request.get_json() or {}
    model = data.get("model", "")
    if model:
        os.environ["LMSTUDIO_MODEL"] = model
    return jsonify({"status": "ok", "model": model})


@app.route("/options")
def options_page():
    base = lmstudio.LMSTUDIO_BASE
    backend = lmstudio.LMSTUDIO_BACKEND
    configured_model = os.environ.get("LMSTUDIO_MODEL", "")
    server_status = "online" if lmstudio.server_is_up(base) else "offline"
    model_state = "unloaded"
    if server_status == "online" and configured_model:
        model_state = "loaded" if lmstudio.model_is_loaded(base, configured_model) else "unloaded"
    available_models = []
    if server_status == "online":
        available_models = lmstudio.get_available_models(base)
    return render_template(
        "options.html",
        backend=backend,
        base_url=base,
        server_status=server_status,
        model_state=model_state,
        configured_model=configured_model,
        available_models=available_models,
        lms_start_error=_lms_start_error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

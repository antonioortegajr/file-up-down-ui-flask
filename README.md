# file-up-down-ui-flask

Photo library management with person tracking and AI-powered confirmation workflow.

## Features

### Library & Upload
- Upload photos to a local library (`uploads/` directory)
- View files as thumbnail grid or list
- View file metadata, EXIF data, and AI-generated descriptions
- Delete uploaded files

### People Management
- Create person profiles with reference photos
- Track which photos contain which people via metadata sidecars
- Edit and delete person records

### Confirmation Engine

The confirmation engine lets you review photos one-by-one to confirm or deny whether a person appears in each photo. Each vote contributes to a confidence score.

**How it works:**
1. Upload reference photos for a person
2. Run AI matching to find candidate photos (`/people/<id>/find` endpoint, requires LM Studio)
3. Use the interactive confirmation UI to vote Yes/No/Skip on each candidate
4. Photos meeting the confidence threshold (default: 60% with at least 2 votes) are marked as **confirmed**
5. View the relationship report to see all reviewed photos with confidence scores

**Confidence scoring:**
- `confidence = yes_votes / (yes_votes + no_votes)`
- A photo is confirmed when `confidence >= 0.6` and `total_votes >= 2`
- Configurable via `CONFIRM_THRESHOLD` and `CONFIRM_MIN_VOTES` environment variables

### Relationship Report

Interactive report showing:
- Total photos reviewed
- Total confirmed photos
- Confirmation rate percentage
- Filter to show confirmed only
- Click any photo to view details
- Download full report as JSON

### LM Studio Integration

When configured with `LMSTUDIO_MODEL`, the app uses LM Studio local LLMs to:
- Auto-describe uploaded photos (background job)
- Find candidate photos for a person based on reference photos
- Show AI match hints during confirmation (why the AI thought it was a match)

## API Endpoints

### Files
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Library homepage |
| POST | `/upload` | Upload files |
| POST | `/delete` | Delete a file |
| GET | `/files` | List files |
| GET | `/file/` | View file details |

### People
| Method | Path | Description |
|--------|------|-------------|
| GET | `/people` | List people |
| GET | `/people/new` | Create person form |
| POST | `/people/new` | Create person |
| GET | `/people/<id>/edit` | Edit person |
| POST | `/people/<id>/edit` | Update person |
| POST | `/people/<id>/delete` | Delete person |
| GET | `/people/<id>` | Person detail |
| GET | `/people/<id>/relationship-report` | Confirmation report |

### Confirmation Session API
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/people/<id>/confirm-session` | Create/resume session |
| GET | `/api/people/<id>/confirm-session/<session_id>` | Get session state |
| POST | `/api/people/<id>/confirm-session/<session_id>/vote` | Submit vote |
| GET | `/people/<id>/interactive-confirm` | Interactive UI |

**Create session:**
```bash
curl -X POST http://localhost:8080/api/people/{person_id}/confirm-session \
  -H "Content-Type: application/json" \
  -d '{"limit": 200}'
```

**Vote:**
```bash
curl -X POST http://localhost:8080/api/people/{person_id}/confirm-session/{session_id}/vote \
  -H "Content-Type: application/json" \
  -d '{"filename": "photo.jpg", "vote": "yes"}'
```

Vote must be `"yes"`, `"no"`, or `"skip"`.

**Response includes:**
- `next_filename` - Next photo to review
- `session_done` - Boolean if queue exhausted
- `confidence` - Updated confidence score
- `yes_votes` / `no_votes` - Vote totals
- `confirmed` - Boolean if photo now meets threshold

### Relationship Report API
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/people/<id>/relationship-map` | Get JSON report |
| GET | `/people/<id>/relationship-report` | Report UI |

**Response shape:**
```json
{
  "person_id": "uuid",
  "person_name": "Name",
  "generated_at": "2024-01-01T00:00:00+00:00",
  "total_photos": 150,
  "confirmed_count": 45,
  "photos": [
    {
      "filename": "photo.jpg",
      "yes_votes": 3,
      "no_votes": 1,
      "confidence": 0.75,
      "confirmed": true,
      "last_session": "2024-01-01T00:00:00+00:00"
    }
  ]
}
```

### Jobs
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/jobs/<job_id>` | SSE progress stream |
| GET | `/api/jobs/<job_id>/result` | Job result |
| POST | `/api/people/<id>/find` | Find person in library |

### LM Studio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/lmstudio/status` | Server/model status |
| POST | `/api/lmstudio/start` | Start model |

### Health
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_FOLDER` | `uploads/` | Upload directory |
| `PEOPLE_FOLDER` | `people/` | People directory |
| `LMSTUDIO_BASE` | `http://127.0.0.1:1234/v1` | LM Studio API base |
| `LMSTUDIO_MODEL` | (none) | Model name to load |
| `LMSTUDIO_API_KEY` | `lm-studio` | API key |
| `CONFIRM_THRESHOLD` | `0.6` | Confidence threshold |
| `CONFIRM_MIN_VOTES` | `2` | Minimum votes to confirm |

## Running

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:8080 in your browser.

For LM Studio features, set `LMSTUDIO_MODEL` before starting:
```bash
export LMSTUDIO_MODEL="model-name"
python app.py
```
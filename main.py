import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from groq_service import GroqError, generate_metadata, get_config
from youtube_service import TranscriptError, extract_video_id, fetch_transcript

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="YouTube Assistant", version="1.0.0")


class GenerateRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=400)
    variations: int = Field(default=3, ge=1, le=5)


class GenerateFromTextRequest(BaseModel):
    transcript: str = Field(..., min_length=50, max_length=120000)
    url: str = Field(default="", max_length=400)
    variations: int = Field(default=3, ge=1, le=5)


def _error(status_code: int, message: str, kind: str = "error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": message, "kind": kind}
    )


@app.get("/api/health")
def health():
    key, model = _config_or_none()
    return {
        "status": "ok",
        "groq_key_configured": key is not None,
        "model": model,
    }


@app.post("/api/generate")
def generate(request: GenerateRequest):
    video_id = extract_video_id(request.url)
    if not video_id:
        return _error(
            400,
            "That is not a valid YouTube link. Paste a video or Short URL, "
            "for example https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    if _config_or_none()[0] is None:
        return _error(
            500,
            "The server is missing GROQ_API_KEY. Set it in the environment "
            "variables and restart the service.",
        )

    try:
        transcript = fetch_transcript(video_id)
    except TranscriptError as exc:
        return _error(422, str(exc), kind=exc.kind)
    except Exception:
        return _error(500, "Something went wrong while reading the transcript.")

    try:
        metadata = generate_metadata(
            transcript.prompt_text,
            f"https://www.youtube.com/watch?v={video_id}",
            variations=request.variations,
        )
    except GroqError as exc:
        return _error(502, str(exc))
    except Exception:
        return _error(500, "Something went wrong while generating the metadata.")

    return {
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "language": transcript.language,
        "source": transcript.source,
        "transcript": transcript.text,
        "variations": metadata["variations"],
        "model": _config_or_none()[1],
    }


@app.post("/api/generate-from-text")
def generate_from_text(request: GenerateFromTextRequest):
    """Manual fallback: the user pastes a transcript they copied from YouTube."""
    video_id = extract_video_id(request.url) if request.url else None
    source_url = (
        f"https://www.youtube.com/watch?v={video_id}" if video_id else "pasted transcript"
    )

    if _config_or_none()[0] is None:
        return _error(
            500,
            "The server is missing GROQ_API_KEY. Set it in the environment "
            "variables and restart the service.",
        )

    try:
        metadata = generate_metadata(
            request.transcript, source_url, variations=request.variations
        )
    except GroqError as exc:
        return _error(502, str(exc))
    except Exception:
        return _error(500, "Something went wrong while generating the metadata.")

    return {
        "video_id": video_id,
        "video_url": source_url if video_id else "",
        "embed_url": f"https://www.youtube.com/embed/{video_id}" if video_id else "",
        "language": "pasted",
        "source": "manual paste",
        "transcript": request.transcript,
        "variations": metadata["variations"],
        "model": _config_or_none()[1],
    }


def _config_or_none():
    try:
        return get_config()
    except GroqError:
        return None, None


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

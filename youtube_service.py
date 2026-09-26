import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests
from youtube_transcript_api import YouTubeTranscriptApi

try:  # module layout differs between 0.6.x and 1.x
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )
except ImportError:  # pragma: no cover
    from youtube_transcript_api import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CAPTION_TRACKS_RE = re.compile(r'"captionTracks"\s*:\s*(\[[^\]]*\])')
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PROMPT_CHAR_LIMIT = 12000


class TranscriptError(Exception):
    """Raised when a transcript cannot be retrieved for a video."""


@dataclass
class Transcript:
    text: str
    language: str
    source: str

    @property
    def prompt_text(self) -> str:
        if len(self.text) <= PROMPT_CHAR_LIMIT:
            return self.text
        return self.text[:PROMPT_CHAR_LIMIT].rsplit(" ", 1)[0] + " ..."


def extract_video_id(raw: str) -> str | None:
    """Pull the 11 character video id out of any common YouTube URL (or raw id)."""
    value = (raw or "").strip()
    if not value:
        return None
    if VIDEO_ID_RE.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None

    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query = parse_qs(parsed.query)
        video_param = query.get("v", [""])[0]
        if VIDEO_ID_RE.match(video_param):
            return video_param
        match = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", parsed.path)
        if match:
            return match.group(1)
    return None


def fetch_transcript(video_id: str) -> Transcript:
    """Return the transcript, trying the library first and a watch page fallback second."""
    errors = []

    for attempt in (_from_library, _from_watch_page):
        try:
            transcript = attempt(video_id)
        except TranscriptError as exc:
            errors.append(str(exc))
            continue
        if transcript and transcript.text.strip():
            return transcript
        errors.append("Transcript was empty.")

    message = errors[-1] if errors else "No transcript is available for this video."
    raise TranscriptError(message)


def _list_transcripts(video_id: str):
    """Works with both the 0.6.x (class methods) and 1.x (instance) APIs."""
    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        return YouTubeTranscriptApi.list_transcripts(video_id)
    return YouTubeTranscriptApi().list(video_id)


def _from_library(video_id: str) -> Transcript:
    try:
        listed = _list_transcripts(video_id)
    except (TranscriptsDisabled, VideoUnavailable) as exc:
        raise TranscriptError(_friendly(exc)) from exc
    except Exception as exc:
        raise TranscriptError(_friendly(exc)) from exc

    try:
        preferred = listed.find_transcript(["en"])
        rows = preferred.fetch()
        return Transcript(_flatten(rows), preferred.language_code, "youtube")
    except NoTranscriptFound:
        pass
    except Exception:
        pass

    try:
        any_transcript = next(iter(listed))
        rows = any_transcript.fetch()
        return Transcript(_flatten(rows), any_transcript.language_code, "youtube")
    except StopIteration:
        raise TranscriptError(
            "This video has no captions at all. Try another video."
        ) from None
    except Exception as exc:
        raise TranscriptError(_friendly(exc)) from exc


def _from_watch_page(video_id: str) -> Transcript:
    """Fallback: scrape captionTracks from the watch page (works when the API is blocked)."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}", headers=headers, timeout=25
        )
    except requests.RequestException as exc:
        raise TranscriptError("Could not reach YouTube. Try again in a moment.") from exc

    if response.status_code != 200:
        raise TranscriptError("YouTube refused the request. Try again shortly.")

    match = CAPTION_TRACKS_RE.search(response.text)
    if not match:
        raise TranscriptError(
            "YouTube is not serving captions for this video right now "
            "(it may be age restricted or private)."
        )

    try:
        tracks = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TranscriptError("Could not read the caption data from YouTube.") from exc
    if not tracks:
        raise TranscriptError("This video has no captions.")

    track = _pick_track(tracks)
    base_url = track.get("baseUrl")
    if not base_url:
        raise TranscriptError("This video has no captions.")

    sep = "&" if "?" in base_url else "?"
    try:
        caption_response = requests.get(
            base_url + sep + "fmt=json3", headers=headers, timeout=25
        )
        caption_response.raise_for_status()
        payload = caption_response.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise TranscriptError("Could not download the captions from YouTube.") from exc

    text = _flatten_json3(payload)
    if not text.strip():
        raise TranscriptError("The transcript for this video came back empty.")
    return Transcript(text, track.get("languageCode", "unknown"), "watch page")


def _pick_track(tracks: list) -> dict:
    for track in tracks:
        if track.get("kind") == "asr":
            continue
        return track
    return tracks[0]


def _flatten(rows) -> str:
    pieces = []
    for row in rows:
        text = row.get("text", "") if isinstance(row, dict) else getattr(row, "text", "")
        if text:
            pieces.append(text.strip())
    return _join(pieces)


def _flatten_json3(payload: dict) -> str:
    pieces = []
    for event in payload.get("events", []):
        segments = event.get("segs") or []
        chunk = "".join(seg.get("utf8", "") for seg in segments)
        if chunk:
            pieces.append(chunk.strip())
    return _join(pieces)


def _join(pieces: list) -> str:
    text = ""
    for piece in pieces:
        if not piece:
            continue
        if text.endswith("\n") or not text:
            text += piece
        else:
            text += " " + piece
    return re.sub(r"[ \t]+", " ", text).strip()


def _friendly(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ("TranscriptsDisabled", "NoTranscriptFound"):
        return "Captions are turned off for this video, so there is no transcript to read."
    if name == "VideoUnavailable":
        return "That video is unavailable, private, or region locked."
    if name == "InvalidVideoId":
        return "That does not look like a valid YouTube video."
    if "429" in str(exc) or "Too Many Requests" in str(exc):
        return "YouTube is rate limiting this server right now. Wait a few seconds and retry."
    return f"Could not read the transcript: {name}."

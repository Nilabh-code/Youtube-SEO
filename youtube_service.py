import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

try:
    from youtube_transcript_api import (
        AgeRestricted,
        IpBlocked,
        NoTranscriptFound,
        PoTokenRequired,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
        YouTubeTranscriptApi,
    )
except ImportError:  # pragma: no cover
    from youtube_transcript_api._errors import (
        AgeRestricted,
        IpBlocked,
        NoTranscriptFound,
        PoTokenRequired,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
    )
    from youtube_transcript_api import YouTubeTranscriptApi

try:
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:  # pragma: no cover
    GenericProxyConfig = None

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CAPTION_TRACKS_RE = re.compile(r'"captionTracks"\s*:\s*(\[[^\]]*\])')
PLAYER_RESPONSE_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\})\s*;", re.DOTALL)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}
PROMPT_CHAR_LIMIT = 12000
WATCH_RETRIES = 3


class TranscriptError(Exception):
    """Raised when a transcript cannot be retrieved.

    `kind` is one of:
      - "blocked"      YouTube blocked this server's IP (retry later / paste manually)
      - "no_captions"  the video simply has no captions
      - "unavailable"  private / deleted / region locked / age restricted / bad id
    """

    def __init__(self, message: str, kind: str = "fetch_failed"):
        super().__init__(message)
        self.kind = kind


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
    """Return the transcript, chaining the library, retries and a scrape fallback."""
    attempts: list[TranscriptError] = []

    try:
        return _from_library(video_id)
    except TranscriptError as exc:
        attempts.append(exc)

    # Only retry the network scrape when the failure was blocking related.
    # Missing captions will not appear by retrying.
    if attempts and attempts[-1].kind in ("blocked", "fetch_failed"):
        try:
            return _from_watch_page(video_id)
        except TranscriptError as exc:
            attempts.append(exc)

    raise _pick_error(attempts)


def _pick_error(attempts: list[TranscriptError]) -> TranscriptError:
    if not attempts:
        return TranscriptError("No transcript is available for this video.", "no_captions")
    # Prefer "unavailable" and "no_captions" verdicts over generic blocking noise,
    # because they describe the video itself rather than our server.
    priority = {"unavailable": 0, "no_captions": 1, "blocked": 2, "fetch_failed": 3}
    return sorted(attempts, key=lambda err: priority.get(err.kind, 3))[0]


def _proxy_config():
    raw = os.getenv("TRANSCRIPT_PROXY", "").strip()
    if not raw or GenericProxyConfig is None:
        return None
    try:
        return GenericProxyConfig(http_url=raw, https_url=raw)
    except Exception:
        return None


def _from_library(video_id: str) -> Transcript:
    try:
        api = YouTubeTranscriptApi(proxy_config=_proxy_config())
    except TypeError:  # very old library without proxy support
        api = YouTubeTranscriptApi()

    try:
        listed = api.list(video_id)
    except (RequestBlocked, IpBlocked, PoTokenRequired) as exc:
        raise TranscriptError(
            "YouTube is blocking transcript requests from this server's IP address "
            "(cloud hosting IPs are routinely blocked). Wait a few minutes and retry, "
            "or paste the transcript manually below.",
            "blocked",
        ) from exc
    except (VideoUnavailable, VideoUnplayable) as exc:
        raise TranscriptError(
            "That video is unavailable, private, deleted, or region locked.",
            "unavailable",
        ) from exc
    except AgeRestricted as exc:
        raise TranscriptError(
            "That video is age restricted, so its transcript cannot be fetched "
            "automatically. Paste the transcript manually below.",
            "unavailable",
        ) from exc
    except TranscriptsDisabled as exc:
        raise TranscriptError(
            "Captions are turned off for this video, so there is no transcript to read.",
            "no_captions",
        ) from exc
    except NoTranscriptFound as exc:
        raise TranscriptError(
            "This video has no captions in any language.",
            "no_captions",
        ) from exc
    except Exception as exc:
        if "429" in str(exc) or "Too Many Requests" in str(exc):
            raise TranscriptError(
                "YouTube is rate limiting this server right now. Wait a minute and retry, "
                "or paste the transcript manually below.",
                "blocked",
            ) from exc
        raise TranscriptError(
            f"Could not read the transcript ({type(exc).__name__}).",
            "fetch_failed",
        ) from exc

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
    except StopIteration:
        raise TranscriptError(
            "This video has no captions in any language.", "no_captions"
        ) from None
    try:
        rows = any_transcript.fetch()
    except Exception as exc:
        raise TranscriptError(
            f"Could not download the captions ({type(exc).__name__}).",
            "fetch_failed",
        ) from exc
    return Transcript(_flatten(rows), any_transcript.language_code, "youtube")


def _from_watch_page(video_id: str) -> Transcript:
    """Fallback: scrape captionTracks from the watch page (helps when the API is throttled)."""
    last_error: TranscriptError | None = None

    for attempt in range(WATCH_RETRIES):
        user_agent = DESKTOP_UA if attempt % 2 == 0 else MOBILE_UA
        try:
            transcript = _scrape_watch_page(video_id, user_agent)
        except TranscriptError as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
            continue
        if transcript and transcript.text.strip():
            return transcript
        last_error = TranscriptError(
            "The transcript for this video came back empty.", "no_captions"
        )
        time.sleep(1.2 * (attempt + 1))

    raise last_error or TranscriptError(
        "Could not read the transcript from YouTube.", "blocked"
    )


def _scrape_watch_page(video_id: str, user_agent: str) -> Transcript:
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}&hl=en&gl=US",
            headers=headers,
            cookies=CONSENT_COOKIES,
            timeout=25,
        )
    except requests.RequestException as exc:
        raise TranscriptError(
            "Could not reach YouTube. Check your connection and retry.", "blocked"
        ) from exc

    if response.status_code == 429:
        raise TranscriptError(
            "YouTube is rate limiting this server right now. Wait a minute and retry, "
            "or paste the transcript manually below.",
            "blocked",
        )
    if response.status_code != 200:
        raise TranscriptError(
            "YouTube refused the request. Try again shortly.", "blocked"
        )

    lowered = response.text.lower()
    if "confirm you're not a bot" in lowered or "sign in to confirm" in lowered:
        raise TranscriptError(
            "YouTube showed a bot check to this server instead of the video "
            "(common on cloud hosting IPs). Wait a few minutes and retry, "
            "or paste the transcript manually below.",
            "blocked",
        )

    match = CAPTION_TRACKS_RE.search(response.text)
    if not match:
        if "ytinitialplayerresponse" not in lowered:
            raise TranscriptError(
                "YouTube did not return video data to this server "
                "(it may be blocking this IP). Wait a bit and retry, "
                "or paste the transcript manually below.",
                "blocked",
            )
        raise TranscriptError(
            "This video has no captions available.", "no_captions"
        )

    try:
        tracks = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            "Could not read the caption data from YouTube.", "blocked"
        ) from exc
    if not tracks:
        raise TranscriptError("This video has no captions available.", "no_captions")

    track = _pick_track(tracks)
    base_url = track.get("baseUrl")
    if not base_url:
        raise TranscriptError("This video has no captions available.", "no_captions")

    sep = "&" if "?" in base_url else "?"
    try:
        caption_response = requests.get(
            base_url + sep + "fmt=json3",
            headers=headers,
            cookies=CONSENT_COOKIES,
            timeout=25,
        )
        caption_response.raise_for_status()
        payload = caption_response.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise TranscriptError(
            "Could not download the captions from YouTube.", "blocked"
        ) from exc

    text = _flatten_json3(payload)
    if not text.strip():
        raise TranscriptError(
            "The transcript for this video came back empty.", "no_captions"
        )
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
            pieces.append(str(text).strip())
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

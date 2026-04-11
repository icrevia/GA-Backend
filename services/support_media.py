from __future__ import annotations

import asyncio
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiofiles
from sqlalchemy import select

from core.config import settings
from core.database import SessionLocal
from models.support import ChatMessage

logger = logging.getLogger("GamerzAdda.support_media")


PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp"
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp", ".m4v", ".mpeg", ".mpg"
}


@dataclass
class StoredSupportMedia:
    media_type: str
    mime_type: str
    size_bytes: int
    relative_path: str
    public_url: str
    expires_at: datetime


class SupportMediaValidationError(ValueError):
    pass


def _now_utc_naive() -> datetime:
    return datetime.utcnow()


def _storage_root() -> Path:
    root = Path(settings.SUPPORT_MEDIA_STORAGE_DIR).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def ensure_support_media_storage_dir() -> Path:
    root = _storage_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "videos").mkdir(parents=True, exist_ok=True)
    return root


def _normalize_relative_path(relative_path: str) -> str:
    return relative_path.replace("\\", "/").lstrip("/")


def _public_url_for(relative_path: str) -> str:
    clean_rel = _normalize_relative_path(relative_path)

    if settings.SUPPORT_MEDIA_PUBLIC_BASE_URL:
        return f"{settings.SUPPORT_MEDIA_PUBLIC_BASE_URL.rstrip('/')}/{clean_rel}"

    prefix = (settings.SUPPORT_MEDIA_PUBLIC_PREFIX or "/static/support_media").strip() or "/static/support_media"
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    url_path = f"{prefix.rstrip('/')}/{clean_rel}"

    if settings.APP_URL:
        return f"{settings.APP_URL.rstrip('/')}{url_path}"
    return url_path


def _detect_media_type(content_type: str | None, filename: str | None) -> str | None:
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()

    if normalized_type.startswith("image/") or suffix in PHOTO_EXTENSIONS:
        return "photo"
    if normalized_type.startswith("video/") or suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def _guess_extension(content_type: str | None, filename: str | None, media_type: str) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower())
    if guessed:
        return guessed.lower()

    suffix = Path(filename or "").suffix.lower()
    if suffix:
        return suffix

    return ".jpg" if media_type == "photo" else ".mp4"


def _max_size_for(media_type: str) -> int:
    if media_type == "photo":
        return int(settings.SUPPORT_MEDIA_PHOTO_MAX_MB) * 1024 * 1024
    return int(settings.SUPPORT_MEDIA_VIDEO_MAX_MB) * 1024 * 1024


def _max_size_label_for(media_type: str) -> str:
    if media_type == "photo":
        return f"{settings.SUPPORT_MEDIA_PHOTO_MAX_MB}MB"
    return f"{settings.SUPPORT_MEDIA_VIDEO_MAX_MB}MB"


def _safe_absolute_media_path(relative_path: str) -> Path:
    root = _storage_root()
    candidate = (root / _normalize_relative_path(relative_path)).resolve()

    if candidate != root and root not in candidate.parents:
        raise ValueError("Unsafe media path")
    return candidate


async def store_support_media(upload_file, owner_user_id: int, sender_role: str) -> StoredSupportMedia:
    media_type = _detect_media_type(upload_file.content_type, upload_file.filename)
    if media_type is None:
        raise SupportMediaValidationError("Only photo and video files are allowed")

    max_size_bytes = _max_size_for(media_type)
    max_size_label = _max_size_label_for(media_type)
    extension = _guess_extension(upload_file.content_type, upload_file.filename, media_type)
    media_folder = "images" if media_type == "photo" else "videos"

    now = _now_utc_naive()
    relative_path = (
        f"{media_folder}/{now.strftime('%Y/%m/%d')}/"
        f"{sender_role.lower()}_{owner_user_id}_{now.strftime('%H%M%S')}_{uuid4().hex[:16]}{extension}"
    )

    destination = _safe_absolute_media_path(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    size_bytes = 0
    try:
        async with aiofiles.open(destination, "wb") as out:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > max_size_bytes:
                    raise SupportMediaValidationError(
                        f"{media_type.capitalize()} size exceeds {max_size_label} limit"
                    )
                await out.write(chunk)
    except Exception:
        try:
            if destination.exists():
                destination.unlink()
        except Exception:
            pass
        raise
    finally:
        try:
            await upload_file.close()
        except Exception:
            pass

    if size_bytes <= 0:
        try:
            if destination.exists():
                destination.unlink()
        except Exception:
            pass
        raise SupportMediaValidationError("Uploaded file is empty")

    expires_at = now + timedelta(hours=int(settings.SUPPORT_MEDIA_RETENTION_HOURS))
    clean_relative_path = _normalize_relative_path(relative_path)

    return StoredSupportMedia(
        media_type=media_type,
        mime_type=(upload_file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower(),
        size_bytes=size_bytes,
        relative_path=clean_relative_path,
        public_url=_public_url_for(clean_relative_path),
        expires_at=expires_at,
    )


def _delete_media_file(relative_path: str) -> bool:
    try:
        target = _safe_absolute_media_path(relative_path)
    except Exception:
        logger.warning("Skipping unsafe support media path during cleanup: %s", relative_path)
        return False

    if not target.exists():
        return False

    try:
        target.unlink()
        return True
    except Exception as exc:
        logger.warning("Failed to delete support media file %s: %s", target, exc)
        return False


async def cleanup_expired_support_media_once(batch_size: int = 500) -> int:
    now = _now_utc_naive()
    async with SessionLocal() as db:
        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.media_path.isnot(None))
            .where(ChatMessage.media_expires_at.isnot(None))
            .where(ChatMessage.media_expires_at <= now)
            .order_by(ChatMessage.media_expires_at.asc(), ChatMessage.id.asc())
            .limit(batch_size)
        )
        expired_messages = result.scalars().all()

        if not expired_messages:
            return 0

        deleted_files = 0
        for msg in expired_messages:
            if msg.media_path and _delete_media_file(msg.media_path):
                deleted_files += 1

            msg.media_type = None
            msg.media_url = None
            msg.media_path = None
            msg.media_mime_type = None
            msg.media_size_bytes = None
            msg.media_expires_at = None
            if not (msg.content or "").strip():
                msg.content = "Media expired."

        await db.commit()

        logger.info(
            "Support media cleanup processed=%s deleted_files=%s",
            len(expired_messages),
            deleted_files,
        )
        return len(expired_messages)


async def support_media_cleanup_worker() -> None:
    interval_seconds = max(1, int(settings.SUPPORT_MEDIA_CLEANUP_INTERVAL_MINUTES)) * 60
    while True:
        try:
            total_cleaned = 0
            while True:
                cleaned = await cleanup_expired_support_media_once(batch_size=500)
                total_cleaned += cleaned
                if cleaned < 500:
                    break
            if total_cleaned > 0:
                logger.info("Support media cleanup cycle completed: cleaned=%s", total_cleaned)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Support media cleanup cycle failed: %s", exc)

        await asyncio.sleep(interval_seconds)

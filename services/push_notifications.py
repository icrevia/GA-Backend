"""
FCM Push Notification Service — Railway/Cloud compatible
Reads Firebase credentials from FIREBASE_SERVICE_ACCOUNT_JSON env var (JSON string).
No file needed on server.
"""

import json
import logging
import os
from typing import Optional

from services.notification_text import append_firebase_suffix

logger = logging.getLogger(__name__)

_fcm_app = None


def _token_hint(token: str) -> str:
    if not token:
        return "None"
    if len(token) <= 12:
        return token
    return f"{token[:8]}...{token[-4:]}"


def _extract_error_code(exc: Exception) -> str:
    code_attr = getattr(exc, "code", None)
    code_value = None
    if callable(code_attr):
        try:
            code_value = code_attr()
        except Exception:
            code_value = None
    elif code_attr is not None:
        code_value = code_attr

    if not code_value:
        code_value = exc.__class__.__name__
    return str(code_value)


def _is_stale_token_error(code: str, detail: str) -> bool:
    blob = f"{code} {detail}".lower()
    markers = [
        "registration-token-not-registered",
        "notregistered",
        "unregistered",
        "requested entity was not found",
        "invalid registration token",
    ]
    return any(marker in blob for marker in markers)


def _get_app():
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app

    try:
        import firebase_admin
        from firebase_admin import credentials

        # ── Try env var first (Railway/cloud deployments) ────────
        sa_json_str = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        if sa_json_str:
            sa_dict = json.loads(sa_json_str)
            cred = credentials.Certificate(sa_dict)
            _fcm_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialised from env var ✅")
            return _fcm_app

        # ── Fallback: local file (local dev only) ────────────────
        sa_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "firebase-service-account.json")
        )
        if os.path.exists(sa_path):
            cred = credentials.Certificate(sa_path)
            _fcm_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialised from file ✅")
            return _fcm_app

        logger.warning(
            "FCM disabled: set FIREBASE_SERVICE_ACCOUNT_JSON env var on Railway."
        )
    except Exception as e:
        logger.error("Firebase Admin init failed: %s", e)

    return None


def send_push(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> bool:
    """Send a push notification to one device. Returns True on success."""
    app = _get_app()
    if app is None:
        return False
    try:
        from firebase_admin import messaging
        title = append_firebase_suffix(title)
        body = append_firebase_suffix(body)
        payload_data = {str(k): str(v) for k, v in (data or {}).items()}
        if image_url:
            payload_data.setdefault("image_url", image_url)
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body, image=image_url or None),
            data=payload_data,
            token=fcm_token,
            android=messaging.AndroidConfig(priority="high"),
        )
        messaging.send(msg, app=app)
        return True
    except Exception as e:
        # Avoid logging the full token for privacy, but show enough to identify
        token_hint = f"{fcm_token[:8]}...{fcm_token[-4:]}" if fcm_token else "None"
        logger.error(f"FCM single send failed for token {token_hint}: {e}")
        return False


def send_push_to_many(
    fcm_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> int:
    """Send to multiple tokens. Returns success count."""
    result = send_push_to_many_detailed(
        fcm_tokens=fcm_tokens,
        title=title,
        body=body,
        data=data,
        image_url=image_url,
    )
    return int(result["success_count"])


def send_push_to_many_detailed(
    fcm_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> dict:
    """
    Send to multiple tokens and return detailed delivery stats.
    Returned keys: success_count, total_count, failure_count, invalid_tokens.
    """
    if not fcm_tokens:
        return {
            "success_count": 0,
            "total_count": 0,
            "failure_count": 0,
            "invalid_tokens": [],
        }

    app = _get_app()
    if app is None:
        total = len([t for t in fcm_tokens if (t or "").strip()])
        return {
            "success_count": 0,
            "total_count": total,
            "failure_count": total,
            "invalid_tokens": [],
        }

    try:
        from firebase_admin import messaging
        title = append_firebase_suffix(title)
        body = append_firebase_suffix(body)

        # Normalize and deduplicate tokens before talking to FCM.
        normalized_tokens: list[str] = []
        seen_tokens = set()
        for raw in fcm_tokens:
            token = (raw or "").strip()
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            normalized_tokens.append(token)

        if not normalized_tokens:
            return {
                "success_count": 0,
                "total_count": 0,
                "failure_count": 0,
                "invalid_tokens": [],
            }

        payload_data = {str(k): str(v) for k, v in (data or {}).items()}
        if image_url:
            payload_data.setdefault("image_url", image_url)

        # Limit batch size to 500 (Firebase Admin SDK limit for send_each)
        batch_size = 500
        total_success = 0
        total_failures = 0
        invalid_tokens: list[str] = []
        
        for i in range(0, len(normalized_tokens), batch_size):
            chunk = normalized_tokens[i : i + batch_size]
            messages = [
                messaging.Message(
                    notification=messaging.Notification(title=title, body=body, image=image_url or None),
                    data=payload_data,
                    token=token,
                    android=messaging.AndroidConfig(priority="high"),
                )
                for token in chunk
            ]
            resp = messaging.send_each(messages, app=app)
            total_success += resp.success_count
            total_failures += resp.failure_count
            
            if resp.failure_count > 0:
                chunk_index = i // batch_size + 1
                logger.warning(
                    "FCM Batch failure: %s messages failed in chunk %s",
                    resp.failure_count,
                    chunk_index,
                )

                # Log concrete failure reasons (capped) and detect stale tokens.
                logged = 0
                for idx, send_result in enumerate(resp.responses):
                    if send_result.success:
                        continue
                    token = chunk[idx]
                    exc = send_result.exception
                    if exc is None:
                        continue

                    error_code = _extract_error_code(exc)
                    error_detail = str(exc)
                    if logged < 5:
                        logger.warning(
                            "FCM send failed for token %s: code=%s detail=%s",
                            _token_hint(token),
                            error_code,
                            error_detail,
                        )
                    logged += 1

                    if _is_stale_token_error(error_code, error_detail):
                        invalid_tokens.append(token)

        logger.info(
            "FCM Broadcast complete: %s/%s sent successfully",
            total_success,
            len(normalized_tokens),
        )

        if invalid_tokens:
            logger.info(
                "FCM detected %s stale/invalid tokens",
                len(set(invalid_tokens)),
            )

        return {
            "success_count": total_success,
            "total_count": len(normalized_tokens),
            "failure_count": total_failures,
            "invalid_tokens": list(set(invalid_tokens)),
        }
    except Exception as e:
        logger.error(f"FCM broadcast send failed: {e}")
        total = len([t for t in fcm_tokens if (t or "").strip()])
        return {
            "success_count": 0,
            "total_count": total,
            "failure_count": total,
            "invalid_tokens": [],
        }

"""
FCM Push Notification Service — Railway/Cloud compatible
Reads Firebase credentials from FIREBASE_SERVICE_ACCOUNT_JSON env var (JSON string).
No file needed on server.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_fcm_app = None


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
) -> bool:
    """Send a push notification to one device. Returns True on success."""
    app = _get_app()
    if app is None:
        return False
    try:
        from firebase_admin import messaging
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={str(k): str(v) for k, v in (data or {}).items()},
            token=fcm_token,
            android=messaging.AndroidConfig(priority="high"),
        )
        messaging.send(msg, app=app)
        return True
    except Exception as e:
        logger.error("FCM send failed: %s", e)
        return False


def send_push_to_many(
    fcm_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> int:
    """Send to multiple tokens. Returns success count."""
    if not fcm_tokens:
        return 0
    app = _get_app()
    if app is None:
        return 0
    try:
        from firebase_admin import messaging
        messages = [
            messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data={str(k): str(v) for k, v in (data or {}).items()},
                token=token,
                android=messaging.AndroidConfig(priority="high"),
            )
            for token in fcm_tokens
        ]
        resp = messaging.send_each(messages, app=app)
        logger.info("FCM batch: %d/%d sent", resp.success_count, len(fcm_tokens))
        return resp.success_count
    except Exception as e:
        logger.error("FCM batch send failed: %s", e)
        return 0

"""
FCM Push Notification Service
Sends push notifications via Firebase HTTP v1 API.

Setup (one-time):
1. Firebase Console → Project Settings → Service Accounts → Generate private key
2. Save the downloaded JSON as  backend/firebase-service-account.json
3. pip install firebase-admin
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazily initialised — only loads if the service-account file exists
_fcm_app = None


def _get_app():
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app

    sa_path = os.path.join(os.path.dirname(__file__), "..", "firebase-service-account.json")
    sa_path = os.path.normpath(sa_path)

    if not os.path.exists(sa_path):
        logger.warning(
            "FCM disabled: firebase-service-account.json not found at %s", sa_path
        )
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials
        cred = credentials.Certificate(sa_path)
        _fcm_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialised ✅")
    except Exception as e:
        logger.error("Firebase Admin init failed: %s", e)
    return _fcm_app


def send_push(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """Send a single push notification to one device token. Returns True on success."""
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
    """Send to multiple tokens (batch). Returns count of successes."""
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
        return resp.success_count
    except Exception as e:
        logger.error("FCM batch send failed: %s", e)
        return 0

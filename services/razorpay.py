import hmac
import hashlib
import requests
import json
import logging
from core.config import settings

logger = logging.getLogger("zexplay.razorpay")


def _rzp_url(path: str) -> str:
    base = settings.RAZORPAY_API_BASE_URL.rstrip("/")
    return f"{base}{path}"

def create_razorpay_order(amount: float, receipt: str) -> dict:
    """
    Create an order on Razorpay.
    Amount should be in Rupees (converted to Paise internally).
    """
    url = _rzp_url("/v1/orders")
    
    # Razorpay expects amount in paise (1 INR = 100 paise)
    amount_paise = int(amount * 100)
    
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1 # Auto capture
    }
    
    try:
        response = requests.post(
            url,
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to create Razorpay order: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Response: {e.response.text}")
        return None


def get_razorpay_order(order_id: str) -> dict | None:
    """Fetch a Razorpay order by id."""
    try:
        response = requests.get(
            _rzp_url(f"/v1/orders/{order_id}"),
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Razorpay order {order_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None


def get_razorpay_payment(payment_id: str) -> dict | None:
    """Fetch a Razorpay payment by id."""
    try:
        response = requests.get(
            _rzp_url(f"/v1/payments/{payment_id}"),
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Razorpay payment {payment_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None

def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Verify the signature received from Android SDK.
    Signature = HMAC-SHA256(order_id + "|" + payment_id, secret)
    """
    try:
        msg = f"{order_id}|{payment_id}"
        generated_signature = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(generated_signature, signature)
    except Exception as e:
        logger.error(f"Signature verification failed: {e}")
        return False

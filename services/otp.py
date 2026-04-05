"""
Message Central OTP service.
Docs: https://www.messagecentral.com/docs/verify-now/otp
"""
import logging
import httpx
from core.config import settings

logger = logging.getLogger("GamerzAdda.otp")

MC_BASE_URL = "https://cpaas.messagecentral.com"


def _headers() -> dict:
    return {
        "authToken": settings.MC_AUTH_TOKEN,
        "Content-Type": "application/json",
    }


def send_otp(phone_e164: str) -> dict:
    """
    Send a 4-digit OTP to the given E.164 phone number (e.g. +919876543210).
    Returns the MC response dict which contains `verificationId`.
    Raises HTTPException-style RuntimeError on failure.
    """
    # MC expects country code without '+' and mobile without country code
    # e.g. +919876543210 -> countryCode=91, mobileNumber=9876543210
    phone = phone_e164.lstrip("+")
    if phone.startswith("91") and len(phone) == 12:
        country_code = "91"
        mobile = phone[2:]
    else:
        country_code = "91"
        mobile = phone

    url = f"{MC_BASE_URL}/verification/v3/send"
    params = {
        "countryCode": country_code,
        "customerId": settings.MC_CUSTOMER_ID,
        "flowType": "SMS",
        "mobileNumber": mobile,
        "otpLength": 4,
    }
    try:
        resp = httpx.post(url, params=params, headers=_headers(), timeout=10.0)
        data = resp.json()
        logger.info(f"MC send_otp response for {mobile}: {data}")
        if resp.status_code != 200 or str(data.get("responseCode")) != "200":
            raise RuntimeError(data.get("message") or "Failed to send OTP")
        return data  # has data["data"]["verificationId"]
    except httpx.HTTPError as e:
        logger.error(f"MC send_otp HTTP error: {e}")
        raise RuntimeError("SMS gateway unreachable. Try again.")


def verify_otp(verification_id: str, otp_code: str) -> bool:
    """
    Verify OTP against Message Central.
    Returns True if valid, False if invalid.
    """
    url = f"{MC_BASE_URL}/verification/v3/validateOtp"
    params = {
        "verificationId": verification_id,
        "customerId": settings.MC_CUSTOMER_ID,
        "code": otp_code,
    }
    try:
        resp = httpx.get(url, params=params, headers=_headers(), timeout=10.0)
        data = resp.json()
        logger.info(f"MC verify_otp response: {data}")
        return (
            str(data.get("responseCode")) == "200"
            and data.get("data", {}).get("verificationStatus") == "VERIFICATION_COMPLETED"
        )
    except httpx.HTTPError as e:
        logger.error(f"MC verify_otp HTTP error: {e}")
        return False

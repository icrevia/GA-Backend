import logging
import httpx
from core.config import settings

logger = logging.getLogger("GamerzAdda.otp")

# Updated MC Base URL (CPaas) as per latest docs
MC_BASE_URL = "https://cpaas.messagecentral.com"


def _clean_env_value(value: str | None) -> str:
    """Trim whitespace and common accidental wrappers from env secrets."""
    cleaned = str(value or "").strip().strip("\"'")
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def _safe_text_preview(raw: str, max_len: int = 200) -> str:
    text = (raw or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}

def _headers() -> dict:
    token = _clean_env_value(settings.MC_AUTH_TOKEN)
    return {
        "authToken": token,
        "Accept": "application/json"
    }

async def send_otp(phone_e164: str) -> dict:
    """Async send OTP using Message Central VerifyNow V3 API."""
    phone = phone_e164.lstrip("+")
    # For India, ensure 91 is extracted correctly
    if phone.startswith("91") and len(phone) == 12:
        country_code = "91"
        mobile = phone[2:]
    else:
        country_code = "91"
        mobile = phone

    url = f"{MC_BASE_URL}/verification/v3/send"
    
    # Customer ID must be exactly as provided in MC portal
    customer_id = _clean_env_value(settings.MC_CUSTOMER_ID)
    if not customer_id or not _clean_env_value(settings.MC_AUTH_TOKEN):
        raise RuntimeError("OTP Gateway credentials are missing. Set MC_AUTH_TOKEN and MC_CUSTOMER_ID.")
    
    # VerifyNow V3 expects these as URL params (not JSON body).
    params = {
        "countryCode": country_code,
        "customerId": customer_id,
        "flowType": "SMS",
        "mobileNumber": mobile,
        "otpLength": 4,
    }
    
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"MC Send (V3) -> Mobile: {mobile}, CustomerId: {customer_id}")
            resp = await client.post(url, params=params, headers=_headers(), timeout=15.0)
            data = _safe_json(resp)
            
            # If 401, it's explicitly an Auth/Token issue
            if resp.status_code == 401:
                logger.error(
                    "MC AUTH FAILED (401). Check MC_AUTH_TOKEN and MC_CUSTOMER_ID in Railway. Body=%s",
                    _safe_text_preview(resp.text),
                )
                raise RuntimeError("OTP Gateway Authentication Failed (401)")

            # MC can return 400 + REQUEST_ALREADY_EXISTS with same verificationId.
            # Reuse that verificationId instead of failing resend flow.
            if resp.status_code == 400 and str(data.get("responseCode")) == "506":
                verification_id = (data.get("data") or {}).get("verificationId")
                if verification_id:
                    logger.warning(
                        "MC Send duplicate request reused. Mobile=%s VerId=%s",
                        mobile,
                        verification_id,
                    )
                    return data

            if resp.status_code != 200:
                logger.error(
                    "MC Send HTTP %s. Body=%s",
                    resp.status_code,
                    _safe_text_preview(resp.text),
                )
                raise RuntimeError(f"OTP Gateway HTTP {resp.status_code}")

            # MC V3 uses 'responseCode': 200 for success
            if str(data.get("responseCode")) != "200":
                error_msg = data.get("message") or f"MC Error {data.get('responseCode')}"
                logger.error(f"MC ERROR RESPONSE: {data}")
                raise RuntimeError(error_msg)
                
            logger.info(f"MC OTP SENT: {mobile}, VerId: {data.get('data', {}).get('verificationId')}")
            return data
        except Exception as e:
            logger.error(f"MC EXCEPTION: {e}")
            raise RuntimeError(f"SMS Service Error: {str(e)}")

async def verify_otp(verification_id: str, otp_code: str) -> bool:
    """Async verify OTP using V3 endpoint"""
    url = f"{MC_BASE_URL}/verification/v3/validateOtp"
    customer_id = _clean_env_value(settings.MC_CUSTOMER_ID)
    
    params = {
        "verificationId": verification_id,
        "customerId": customer_id,
        "code": otp_code,
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=_headers(), timeout=15.0)
            data = _safe_json(resp)

            if resp.status_code == 401:
                logger.error("MC Verify auth failed (401). Body=%s", _safe_text_preview(resp.text))
                raise RuntimeError("OTP verification provider auth failed")

            # Provider-side outage/transient errors should not be shown as "Invalid OTP".
            if resp.status_code >= 500 or resp.status_code in {408, 429}:
                logger.error("MC Verify HTTP %s. Body=%s", resp.status_code, _safe_text_preview(resp.text))
                raise RuntimeError("OTP verification service unavailable")

            verification_status = str((data.get("data") or {}).get("verificationStatus") or "").upper()
            response_code = str(data.get("responseCode") or "")

            if resp.status_code == 200:
                if response_code == "200" and verification_status == "VERIFICATION_COMPLETED":
                    return True

                # Known user-facing invalid/expired states.
                if verification_status in {
                    "VERIFICATION_FAILED",
                    "OTP_INVALID",
                    "INVALID_OTP",
                    "OTP_MISMATCH",
                    "FAILED",
                    "EXPIRED",
                }:
                    return False

                if response_code in {"702", "703", "704", "705", "1702", "1703", "1704"}:
                    return False

                logger.error("MC Verify unresolved response. Body=%s", _safe_text_preview(resp.text))
                raise RuntimeError("OTP verification service unavailable")

            # Non-200 but non-5xx: map clearly invalid OTP states to False.
            if verification_status in {
                "VERIFICATION_FAILED",
                "OTP_INVALID",
                "INVALID_OTP",
                "OTP_MISMATCH",
                "FAILED",
                "EXPIRED",
            } or response_code in {"702", "703", "704", "705", "1702", "1703", "1704"}:
                return False

            logger.error("MC Verify HTTP %s. Body=%s", resp.status_code, _safe_text_preview(resp.text))
            raise RuntimeError("OTP verification service unavailable")
        except Exception as e:
            logger.error(f"MC Verify Exception: {e}")
            raise RuntimeError(f"OTP verification failed: {str(e)}")

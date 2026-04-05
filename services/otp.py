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
            
            # If 401, it's explicitly an Auth/Token issue
            if resp.status_code == 401:
                logger.error(
                    "MC AUTH FAILED (401). Check MC_AUTH_TOKEN and MC_CUSTOMER_ID in Railway. Body=%s",
                    _safe_text_preview(resp.text),
                )
                raise RuntimeError("OTP Gateway Authentication Failed (401)")

            if resp.status_code != 200:
                logger.error(
                    "MC Send HTTP %s. Body=%s",
                    resp.status_code,
                    _safe_text_preview(resp.text),
                )
                raise RuntimeError(f"OTP Gateway HTTP {resp.status_code}")

            data = resp.json()
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
            if resp.status_code != 200:
                logger.error(f"MC Verify HTTP {resp.status_code}")
                return False
            data = resp.json()
            return (
                str(data.get("responseCode")) == "200"
                and data.get("data", {}).get("verificationStatus") == "VERIFICATION_COMPLETED"
            )
        except Exception as e:
            logger.error(f"MC Verify Exception: {e}")
            return False

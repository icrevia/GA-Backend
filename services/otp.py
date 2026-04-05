import logging
import httpx
from core.config import settings

logger = logging.getLogger("GamerzAdda.otp")

MC_BASE_URL = "https://cpaas.messagecentral.com"

def _headers() -> dict:
    return {
        "authToken": str(settings.MC_AUTH_TOKEN or ""),
        "Content-Type": "application/json",
    }

async def send_otp(phone_e164: str) -> dict:
    """Async send OTP using Message Central"""
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
        "customerId": str(settings.MC_CUSTOMER_ID or ""),
        "flowType": "SMS",
        "mobileNumber": mobile,
        "otpLength": 4,
    }
    
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Sending OTP to {mobile} via Message Central...")
            resp = await client.post(url, params=params, headers=_headers(), timeout=15.0)
            data = resp.json()
            
            if resp.status_code != 200 or str(data.get("responseCode")) != "200":
                error_msg = data.get("message") or f"MC Error {resp.status_code}"
                logger.error(f"OTP SEND FAILED: {error_msg}")
                raise RuntimeError(error_msg)
                
            logger.info(f"OTP SENT SUCCESS: {mobile}")
            return data
        except Exception as e:
            logger.error(f"OTP SERVICE EXCEPTION: {e}")
            raise RuntimeError(f"SMS gateway error: {str(e)}")

async def verify_otp(verification_id: str, otp_code: str) -> bool:
    """Async verify OTP"""
    url = f"{MC_BASE_URL}/verification/v3/validateOtp"
    params = {
        "verificationId": verification_id,
        "customerId": str(settings.MC_CUSTOMER_ID or ""),
        "code": otp_code,
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=_headers(), timeout=15.0)
            data = resp.json()
            return (
                str(data.get("responseCode")) == "200"
                and data.get("data", {}).get("verificationStatus") == "VERIFICATION_COMPLETED"
            )
        except Exception:
            return False

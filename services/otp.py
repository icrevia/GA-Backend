import logging
import httpx
import asyncio
import time
import random
import uuid
import hashlib
from core.config import settings
from core.database import SyncSessionLocal
from sqlalchemy import text

logger = logging.getLogger("GamerzAdda.otp")

# Updated MC Base URL (CPaas) as per latest docs
MC_BASE_URL = "https://cpaas.messagecentral.com"
SM_BASE_URL = "https://api.startmessaging.com"

# Local store for StartMessaging OTPs: verification_id -> {"hash": str, "expires_at": float}
_sm_otp_store: dict[str, dict] = {}

# Local store for HSP OTPs: verification_id -> {"hash": str, "expires_at": float}
_hsp_otp_store: dict[str, dict] = {}

def _get_active_provider() -> str:
    try:
        with SyncSessionLocal() as db:
            result = db.execute(text("SELECT config_value FROM system_configs WHERE config_key = 'OTP_PROVIDER'"))
            row = result.fetchone()
            return str(row[0]).upper().strip() if row else "MESSAGE_CENTRAL"
    except Exception as e:
        logger.error(f"Error reading OTP_PROVIDER from DB: {e}")
        return "MESSAGE_CENTRAL"

def _cleanup_sm_store():
    now = time.time()
    expired = [vid for vid, data in _sm_otp_store.items() if data["expires_at"] < now]
    for vid in expired:
        _sm_otp_store.pop(vid, None)

async def _send_otp_startmessaging(phone_e164: str) -> dict:
    phone = phone_e164.lstrip("+")
    if phone.startswith("91") and len(phone) == 12:
        mobile = phone
    else:
        mobile = f"91{phone}" if len(phone) == 10 else phone
    
    api_key = _clean_env_value(settings.SM_API_KEY)
    if not api_key:
        raise RuntimeError("StartMessaging API Key is missing. Set SM_API_KEY.")
    
    otp_code = str(random.randint(1000, 9999))
    verification_id = str(uuid.uuid4())
    
    _cleanup_sm_store()
    _sm_otp_store[verification_id] = {
        "hash": hashlib.sha256(otp_code.encode()).hexdigest(),
        "expires_at": time.time() + 300  # 5 mins expiry
    }
    
    url = f"{SM_BASE_URL}/otp/send"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key
    }
    payload = {
        "phoneNumber": f"+{mobile}",
        "templateId": "0afbdeb0-785d-4dd0-bd48-365a182df276",
        "variables": {
            "otp": otp_code,
            "appName": "GamerzAdda"
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"SM Send -> Mobile: {mobile}")
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
            data = _safe_json(resp)
            
            if resp.status_code not in (200, 201):
                logger.error(f"SM Send HTTP {resp.status_code}. Body={_safe_text_preview(resp.text)}")
                raise RuntimeError(f"OTP Gateway HTTP {resp.status_code}")
                
            logger.info(f"SM OTP SENT: {mobile}, VerId: {verification_id}")
            # Mock the Message Central response structure
            return {"data": {"verificationId": verification_id}, "responseCode": "200"}
        except Exception as e:
            logger.error(f"SM EXCEPTION: {e}")
            raise RuntimeError(f"SMS Service Error: {str(e)}")

async def _verify_otp_startmessaging(verification_id: str, otp_code: str) -> bool:
    _cleanup_sm_store()
    record = _sm_otp_store.get(verification_id)
    if not record:
        logger.warning(f"SM Verify rejected (Not Found/Expired). VerId={verification_id}")
        return False
        
    expected_hash = record["hash"]
    actual_hash = hashlib.sha256(str(otp_code).encode()).hexdigest()
    
    if expected_hash == actual_hash:
        _sm_otp_store.pop(verification_id, None)
        logger.info(f"SM Verify success. VerId={verification_id}")
        return True
    
    logger.warning(f"SM Verify rejected (Invalid OTP). VerId={verification_id}")
    return False

def _cleanup_hsp_store():
    now = time.time()
    expired = [vid for vid, data in _hsp_otp_store.items() if data["expires_at"] < now]
    for vid in expired:
        _hsp_otp_store.pop(vid, None)

async def _send_otp_hsp(phone_e164: str) -> dict:
    phone = phone_e164.lstrip("+")
    if phone.startswith("91") and len(phone) == 12:
        mobile = phone
    else:
        mobile = f"91{phone}" if len(phone) == 10 else phone

    username = _clean_env_value(settings.HSP_USERNAME)
    api_key = _clean_env_value(settings.HSP_API_KEY)
    sendername = _clean_env_value(settings.HSP_SENDERNAME)
    template = settings.HSP_TEMPLATE

    if not username or not api_key:
        raise RuntimeError("HSP API credentials missing. Set HSP_USERNAME and HSP_API_KEY.")

    otp_code = str(random.randint(1000, 9999))
    verification_id = str(uuid.uuid4())
    message = template.replace("{otp}", otp_code)

    _cleanup_hsp_store()
    _hsp_otp_store[verification_id] = {
        "hash": hashlib.sha256(otp_code.encode()).hexdigest(),
        "expires_at": time.time() + 300  # 5 mins expiry
    }

    url = "http://sms.hspsms.com/sendSMS"
    params = {
        "username": username,
        "message": message,
        "sendername": sendername,
        "smstype": "TRANS",
        "numbers": mobile,
        "apikey": api_key,
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            logger.info(f"HSP Send -> Mobile: {mobile}")
            resp = await client.get(url, params=params, timeout=15.0)
            
            if resp.status_code not in (200, 201):
                logger.error(f"HSP Send HTTP {resp.status_code}. Body={_safe_text_preview(resp.text)}")
                raise RuntimeError(f"OTP Gateway HTTP {resp.status_code}")
                
            logger.info(f"HSP OTP SENT: {mobile}, VerId: {verification_id}")
            return {"data": {"verificationId": verification_id}, "responseCode": "200"}
        except Exception as e:
            logger.error(f"HSP EXCEPTION: {e}")
            raise RuntimeError(f"SMS Service Error: {str(e)}")

async def _verify_otp_hsp(verification_id: str, otp_code: str) -> bool:
    _cleanup_hsp_store()
    record = _hsp_otp_store.get(verification_id)
    if not record:
        logger.warning(f"HSP Verify rejected (Not Found/Expired). VerId={verification_id}")
        return False
        
    expected_hash = record["hash"]
    actual_hash = hashlib.sha256(str(otp_code).encode()).hexdigest()
    
    if expected_hash == actual_hash:
        _hsp_otp_store.pop(verification_id, None)
        logger.info(f"HSP Verify success. VerId={verification_id}")
        return True
    
    logger.warning(f"HSP Verify rejected (Invalid OTP). VerId={verification_id}")
    return False

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

async def _send_otp_message_central(phone_e164: str) -> dict:
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

async def send_otp(phone_e164: str) -> dict:
    provider = _get_active_provider()
    if provider == "START_MESSAGING":
        return await _send_otp_startmessaging(phone_e164)
    elif provider == "HSP":
        return await _send_otp_hsp(phone_e164)
    return await _send_otp_message_central(phone_e164)

async def _verify_otp_message_central(verification_id: str, otp_code: str) -> bool:
    """Async verify OTP using V3 endpoint"""
    url = f"{MC_BASE_URL}/verification/v3/validateOtp"
    customer_id = _clean_env_value(settings.MC_CUSTOMER_ID)
    
    params = {
        "verificationId": verification_id,
        "customerId": customer_id,
        "code": otp_code,
    }
    
    transient_status_codes = {408, 429}
    max_attempts = 3

    async with httpx.AsyncClient() as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.get(url, params=params, headers=_headers(), timeout=15.0)
                data = _safe_json(resp)

                if resp.status_code == 401:
                    logger.error("MC Verify auth failed (401). Body=%s", _safe_text_preview(resp.text))
                    raise RuntimeError("OTP verification provider auth failed")

                # Provider-side outage/transient errors should not be shown as "Invalid OTP".
                if resp.status_code >= 500 or resp.status_code in transient_status_codes:
                    if attempt < max_attempts:
                        backoff = 0.8 * attempt
                        logger.warning(
                            "MC Verify transient HTTP %s (attempt %s/%s). Retrying in %.1fs",
                            resp.status_code,
                            attempt,
                            max_attempts,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue

                    logger.error("MC Verify HTTP %s. Body=%s", resp.status_code, _safe_text_preview(resp.text))
                    raise RuntimeError("OTP verification service unavailable")

                verification_status = str((data.get("data") or {}).get("verificationStatus") or "").upper().strip()
                response_code = str(data.get("responseCode") or "").strip()

                has_failure_marker = (
                    "FAIL" in verification_status
                    or "INVALID" in verification_status
                    or "EXPIR" in verification_status
                    or "REJECT" in verification_status
                )

                has_success_marker = (
                    "COMPLET" in verification_status
                    or (
                        "VERIF" in verification_status
                        and not has_failure_marker
                    )
                    or verification_status in {"SUCCESS", "VERIFIED", "VALID", "VERIFICATION COMPLETED"}
                )

                is_invalid_or_expired = (
                    verification_status in {
                        "VERIFICATION_FAILED",
                        "OTP_INVALID",
                        "INVALID_OTP",
                        "OTP_MISMATCH",
                        "FAILED",
                        "EXPIRED",
                    }
                    or has_failure_marker
                    or response_code in {"702", "703", "704", "705", "1702", "1703", "1704"}
                )

                if resp.status_code == 200:
                    if response_code in {"200", ""} and has_success_marker:
                        logger.info(
                            "MC Verify success. VerId=%s status=%s code=%s",
                            verification_id,
                            verification_status,
                            response_code,
                        )
                        return True

                    # Known user-facing invalid/expired states.
                    if is_invalid_or_expired:
                        logger.warning(
                            "MC Verify rejected. VerId=%s status=%s code=%s",
                            verification_id,
                            verification_status,
                            response_code,
                        )
                        return False

                    if attempt < max_attempts:
                        backoff = 0.8 * attempt
                        logger.warning(
                            "MC Verify unresolved response (attempt %s/%s). Retrying in %.1fs. Body=%s",
                            attempt,
                            max_attempts,
                            backoff,
                            _safe_text_preview(resp.text),
                        )
                        await asyncio.sleep(backoff)
                        continue

                    logger.error("MC Verify unresolved response. Body=%s", _safe_text_preview(resp.text))
                    raise RuntimeError("OTP verification service unavailable")

                # Non-200 but non-5xx: map clearly invalid OTP states to False.
                if is_invalid_or_expired:
                    return False

                if attempt < max_attempts:
                    backoff = 0.8 * attempt
                    logger.warning(
                        "MC Verify non-terminal HTTP %s (attempt %s/%s). Retrying in %.1fs",
                        resp.status_code,
                        attempt,
                        max_attempts,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                logger.error("MC Verify HTTP %s. Body=%s", resp.status_code, _safe_text_preview(resp.text))
                raise RuntimeError("OTP verification service unavailable")

            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt < max_attempts:
                    backoff = 0.8 * attempt
                    logger.warning(
                        "MC Verify network error (attempt %s/%s): %s. Retrying in %.1fs",
                        attempt,
                        max_attempts,
                        e,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                logger.error("MC Verify network exception: %s", e)
                raise RuntimeError("OTP verification service unavailable")

            except Exception as e:
                logger.error(f"MC Verify Exception: {e}")
                raise RuntimeError(f"OTP verification failed: {str(e)}")

    raise RuntimeError("OTP verification service unavailable")

async def verify_otp(verification_id: str, otp_code: str) -> bool:
    provider = _get_active_provider()
    if provider == "START_MESSAGING":
        return await _verify_otp_startmessaging(verification_id, otp_code)
    elif provider == "HSP":
        return await _verify_otp_hsp(verification_id, otp_code)
    return await _verify_otp_message_central(verification_id, otp_code)

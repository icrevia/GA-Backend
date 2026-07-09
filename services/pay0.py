import requests
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

logger = logging.getLogger("GamerzAdda.pay0")


def _normalize_mobile(customer_mobile: str) -> str:
    digits = "".join(ch for ch in str(customer_mobile or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return "9999999999"


def _format_amount(amount: float) -> str:
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        value = Decimal("0.00")
    normalized = format(value.normalize(), "f")
    return normalized if "." in normalized else f"{normalized}.00"


def _safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        return response.json()
    except Exception:
        body_preview = (response.text or "")[:400]
        return {
            "status": False,
            "message": f"Invalid JSON response from Pay0 (HTTP {response.status_code})",
            "raw_body": body_preview,
        }

def create_pay0_order(
    api_key: str,
    order_id: str,
    amount: float,
    customer_name: str,
    customer_mobile: str,
    redirect_url: str
) -> dict:
    """
    Creates a Pay0 payment session and returns the payment URL.
    Returns {"success": True, "payment_url": "...", "raw": ...}
             or {"success": False, "error": "..."}
    """
    url = "https://pay0.shop/api/create-order"

    if not api_key:
        return {"success": False, "error": "PAY0_MERCHANT_KEY is missing"}
    
    payload = {
        "customer_mobile": _normalize_mobile(customer_mobile),
        "customer_name": (customer_name or "GamerzAdda User")[:80],
        "user_token": api_key,
        "amount": _format_amount(amount),
        "order_id": order_id,
        "redirect_url": redirect_url,
        "remark1": "GamerzAdda Wallet Recharge",
        "remark2": order_id,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(url, data=payload, headers=headers, timeout=20)
        data = _safe_json(response)

        if response.status_code >= 400:
            return {
                "success": False,
                "error": data.get("message") or f"Pay0 HTTP {response.status_code}",
                "raw": data,
            }
        
        if data.get("status") is True:
            result = data.get("result", {})
            payment_url = result.get("payment_url") or data.get("payment_url")
            provider_order_id = result.get("orderId") or result.get("order_id") or order_id
            if payment_url:
                return {
                    "success": True,
                    "payment_url": payment_url,
                    "order_id": provider_order_id,
                    "raw": data,
                }
                
        return {
            "success": False,
            "error": data.get("message", "Unknown error from Pay0"),
            "raw": data,
        }
    except Exception as e:
        logger.error(f"Pay0 Create Order Error: {e}")
        return {"success": False, "error": str(e)}


def check_pay0_order_status(api_key: str, order_id: str) -> dict:
    """
    Verifies the payment status of a specific order ID directly from Pay0 servers.
    Returns:
    {
        "status": "SUCCESS" | "FAILED" | "PENDING",
        "amount": float,
        "utr": "...",
        "raw": dict
    }
    """
    url = "https://pay0.shop/api/check-order-status"

    if not api_key:
        return {
            "status": "FAILED",
            "error": "PAY0_MERCHANT_KEY is missing",
            "raw": {},
        }
    
    payload = {
        "user_token": api_key,
        "order_id": order_id
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(url, data=payload, headers=headers, timeout=20)
        data = _safe_json(response)

        if response.status_code >= 400:
            return {
                "status": "FAILED",
                "error": data.get("message") or f"Pay0 HTTP {response.status_code}",
                "raw": data,
            }
        
        if data.get("status") is True:
            result = data.get("result", {})
            txn_status = str(result.get("txnStatus", "")).upper()
            provider_order_id = result.get("orderId") or result.get("order_id") or order_id
            
            # They specify 'SUCCESS' or 'PENDING'
            if txn_status == "SUCCESS":
                return {
                    "status": "SUCCESS",
                    "order_id": provider_order_id,
                    "amount": float(result.get("amount", 0) or 0),
                    "utr": result.get("utr", ""),
                    "raw": data
                }
            elif txn_status == "PENDING":
                return {
                    "status": "PENDING",
                    "order_id": provider_order_id,
                    "raw": data,
                }
            else:
                return {
                    "status": "FAILED",
                    "order_id": provider_order_id,
                    "raw": data,
                }
                
        return {
            "status": "FAILED",
            "error": data.get("message", "Unknown error from Pay0"),
            "raw": data,
        }
    except Exception as e:
        logger.error(f"Pay0 Check Status Error: {e}")
        return {"status": "FAILED", "raw": {"error": str(e)}}

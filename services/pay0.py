import requests
import logging

logger = logging.getLogger("GamerzAdda.pay0")

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
    
    payload = {
        "customer_mobile": customer_mobile,
        "customer_name": customer_name,
        "user_token": api_key,
        "amount": str(int(amount) if amount.is_integer() else amount),
        "order_id": order_id,
        "redirect_url": redirect_url,
        "remark1": "GamerzAdda Wallet Recharge"
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        data = response.json()
        
        if data.get("status") is True:
            result = data.get("result", {})
            payment_url = result.get("payment_url")
            if payment_url:
                return {"success": True, "payment_url": payment_url, "raw": data}
                
        return {"success": False, "error": data.get("message", "Unknown error from Pay0")}
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
    
    payload = {
        "user_token": api_key,
        "order_id": order_id
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        data = response.json()
        
        if data.get("status") is True:
            result = data.get("result", {})
            txn_status = str(result.get("txnStatus", "")).upper()
            
            # They specify 'SUCCESS' or 'PENDING'
            if txn_status == "SUCCESS":
                return {
                    "status": "SUCCESS",
                    "amount": float(result.get("amount", 0)),
                    "utr": result.get("utr", ""),
                    "raw": data
                }
            elif txn_status == "PENDING":
                return {"status": "PENDING", "raw": data}
            else:
                return {"status": "FAILED", "raw": data}
                
        return {"status": "FAILED", "raw": data}
    except Exception as e:
        logger.error(f"Pay0 Check Status Error: {e}")
        return {"status": "FAILED", "raw": {"error": str(e)}}

from fastapi import APIRouter, Depends, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import List
from decimal import Decimal, InvalidOperation
import uuid
import html
import logging
import hashlib

from api.deps import get_db, get_current_user, get_current_active_admin
from models.user import User
from models.wallet import WalletTransaction
from services.pay0 import create_pay0_order, check_pay0_order_status
from schemas.wallet import AddMoneyRequest, PaymentInitResponse, WithdrawalRequest, WalletTransactionResponse, WalletBalanceResponse
from core.config import settings
from services.notifications import add_user_notification
from core.websockets import manager as ws_manager

logger = logging.getLogger("GamerzAdda.wallet")

router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# Wallet balance & history
# ─────────────────────────────────────────────────────────────────

@router.get("/balance", response_model=WalletBalanceResponse)
def get_balance(current_user: User = Depends(get_current_user)):
    return {"balance": current_user.wallet_balance}


@router.get("/transactions", response_model=List[WalletTransactionResponse])
def get_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == current_user.id)
        .order_by(WalletTransaction.created_at.desc())
        .all()
    )


# ─────────────────────────────────────────────────────────────────
# Initiate a payment (Pay0.shop)
# ─────────────────────────────────────────────────────────────────

@router.post("/add-money/init", response_model=PaymentInitResponse)
def init_add_money(
    req: AddMoneyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if req.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum recharge amount is ₹1")
    if req.amount > 100_000:
        raise HTTPException(status_code=400, detail="Maximum recharge amount is ₹1,00,000")

    txnid = f"GA_{uuid.uuid4().hex[:12].upper()}"

    tx = WalletTransaction(
        user_id=current_user.id,
        amount=req.amount,
        transaction_type="ADD_MONEY",
        status="PENDING",
        reference_id=txnid,
        payment_mode="PAY0"
    )
    db.add(tx)
    db.flush()

    api_key = settings.PAY0_MERCHANT_KEY
    redirect_url = f"{settings.APP_URL}/api/v1/wallet/pay0/return"
    
    try:
        pay0_res = create_pay0_order(
            api_key=api_key,
            order_id=txnid,
            amount=float(req.amount),
            customer_name=current_user.username,
            customer_mobile=current_user.phone_number or "9999999999",
            redirect_url=redirect_url
        )
        
        if not pay0_res.get("success"):
            raise RuntimeError(f"PAY0_INIT_FAILED: {pay0_res.get('error', 'Unknown Error')}")

        tx.gateway_order_id = txnid
        
        response_payload = {
            "gateway": "PAY0",
            "pay0_init": {
                "payment_url": pay0_res["payment_url"],
                "order_id": txnid
            }
        }
    except Exception as exc:
        db.rollback()
        failure_reason = str(exc)

        failed_tx = WalletTransaction(
            user_id=current_user.id,
            amount=req.amount,
            transaction_type="ADD_MONEY",
            status="FAILED",
            reference_id=txnid,
            payment_mode="PAY0",
            failure_reason=failure_reason,
        )
        db.add(failed_tx)
        db.commit()

        add_user_notification(
            db,
            current_user.id,
            "Recharge Failed ❌",
            f"We could not initialize your payment via Pay0. {failure_reason}",
            "WALLET"
        )
        logger.error("Add-money init failed for user=%s reason=%s", current_user.id, failure_reason)

        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=f"Failed to initialize Pay0: {failure_reason}")

    db.add(tx)
    db.commit()

    add_user_notification(
        db,
        current_user.id,
        "Recharge Initiated",
        f"You have initiated a recharge of ₹{req.amount} via Pay0. Complete the payment to see it in your wallet.",
        "WALLET"
    )

    return response_payload


# ─────────────────────────────────────────────────────────────────
# Pay0 Webhook & Post-Payment Redirect
# ─────────────────────────────────────────────────────────────────

@router.post("/pay0/webhook")
@router.post("/pay0/return")
@router.get("/pay0/return", response_class=HTMLResponse)
async def pay0_callback_handler(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Handles both server-to-server webhooks and user browser redirects from Pay0.shop.
    Uses direct API check-order-status for maximum security.
    """
    if request.method == "POST":
        form_data = dict(await request.form())
    else:
        form_data = dict(request.query_params)

    order_id = form_data.get("order_id")
    if not order_id:
        return HTMLResponse("<body>Invalid Request: Missing order_id</body>", status_code=400)

    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == order_id
    ).with_for_update().first()

    if not tx:
        return HTMLResponse("<body>Transaction not found</body>", status_code=404)

    # Strictly verify against Pay0 Check Status API to prevent spoofing
    api_key = settings.PAY0_MERCHANT_KEY
    status_res = check_pay0_order_status(api_key, order_id)
    
    final_status = "PENDING"
    
    if status_res["status"] == "SUCCESS":
        final_status = "success"
        if tx.status == "PENDING":
            tx.status = "SUCCESS"
            tx.gateway_payment_id = status_res.get("utr") or form_data.get("utr")
            user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
            user.wallet_balance += tx.amount
            db.add(user)
            
            add_user_notification(
                db, user.id,
                "Payment Confirmed ✅",
                f"₹{tx.amount:.0f} has been added to your wallet via Pay0.",
                "WALLET"
            )
            background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    
    elif status_res["status"] == "FAILED":
        final_status = "failed"
        if tx.status == "PENDING":
            tx.status = "FAILED"
            tx.failure_reason = "PAY0_CONFIRMED_FAILED"
            add_user_notification(
                db, tx.user_id,
                "Recharge Failed ❌",
                f"Your payment of ₹{tx.amount} has failed.",
                "WALLET"
            )
            background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
            
    db.add(tx)
    db.commit()

    if "/webhook" in str(request.url):
        return {"message": "Webhook processed", "status": final_status}

    bg_color = "#16A34A" if final_status == "success" else ("#F59E0B" if final_status == "PENDING" else "#EF4444")
    label = "Payment Successful!" if final_status == "success" else ("Payment Pending..." if final_status == "PENDING" else "Payment Failed!")
    
    return HTMLResponse(f"""<!DOCTYPE html>
<html><body style="background:#0D0E12; color:white; text-align:center; padding-top:50px; font-family:sans-serif;">
    <h2 style="color:{html.escape(bg_color)};">{html.escape(label)}</h2>
    <p>You can now close this screen and return to the app.</p>
</body></html>""")


# ─────────────────────────────────────────────────────────────────
# Status Polling
# ─────────────────────────────────────────────────────────────────

@router.get("/status/{txnid}")
def get_payment_status(
    txnid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Polls the database for the final status of a transaction.
    """
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == txnid,
        WalletTransaction.user_id == current_user.id
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    if tx.status == "PENDING":
        status_res = check_pay0_order_status(settings.PAY0_MERCHANT_KEY, txnid)
        if status_res["status"] == "SUCCESS":
            tx.status = "SUCCESS"
            tx.gateway_payment_id = status_res.get("utr")
            user = db.query(User).filter(User.id == tx.user_id).first()
            user.wallet_balance += tx.amount
            db.commit()

    return {
        "txnid": txnid,
        "status": tx.status,
        "amount": tx.amount,
        "payment_mode": tx.payment_mode,
        "failure_reason": tx.failure_reason,
        "utr": tx.gateway_payment_id
    }


# ─────────────────────────────────────────────────────────────────
# Withdrawal Logic
# ─────────────────────────────────────────────────────────────────

@router.post("/withdraw")
def request_withdrawal(
    req: WithdrawalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if req.amount > 50_000:
        raise HTTPException(status_code=400, detail="Maximum withdrawal per request is ₹50,000")

    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()

    if user.wallet_balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    user.wallet_balance -= req.amount
    user.upi_id = req.upi_id

    tx = WalletTransaction(
        user_id=user.id,
        amount=-req.amount,
        transaction_type="WITHDRAWAL",
        status="PENDING",
        reference_id=f"WITHDRAW_{uuid.uuid4().hex[:8].upper()}",
        gateway_payment_id=req.upi_id
    )

    db.add(tx)
    db.add(user)
    db.commit()

    add_user_notification(
        db,
        user.id,
        "Withdrawal Requested",
        f"Your withdrawal request of ₹{req.amount} has been submitted.",
        "WALLET"
    )

    return {"message": "Withdrawal requested successfully."}

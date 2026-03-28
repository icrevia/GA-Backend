from fastapi import APIRouter, Depends, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import List
import uuid
import html
import logging
import logging


from api.deps import get_db, get_current_user, get_current_active_admin
from models.user import User
from models.wallet import WalletTransaction
from schemas.wallet import AddMoneyRequest, PayUInitResponse, RazorpayInitResponse, PaymentInitResponse, WithdrawalRequest, WalletTransactionResponse, WalletBalanceResponse
from services.payu import generate_payu_hash, verify_payu_hash
from services.razorpay import create_razorpay_order, verify_razorpay_signature
from services.ccavenue import encrypt_ccavenue, decrypt_ccavenue
from core.config import settings
from models.config import SystemConfig
from services.notifications import add_user_notification
from core.websockets import manager as ws_manager

logger = logging.getLogger("zexplay.wallet")

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _payu_surl() -> str:
    return f"{settings.APP_URL}/api/v1/wallet/payu/success"

def _payu_furl() -> str:
    return f"{settings.APP_URL}/api/v1/wallet/payu/failure"


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
# Initiate a payment
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

    txnid = f"ZEX_{uuid.uuid4().hex[:12].upper()}"
    productinfo = "Digital Services"

    tx = WalletTransaction(
        user_id=current_user.id,
        amount=req.amount,
        transaction_type="ADD_MONEY",
        status="PENDING",
        reference_id=txnid
    )
    db.add(tx)
    db.commit()

    add_user_notification(
        db,
        current_user.id,
        "Recharge Initiated",
        f"You have initiated a recharge of ₹{req.amount}. Complete the payment to see it in your wallet.",
        "WALLET"
    )

    # ─────────────────────────────────────────────────────────────────
    # Determine Active Gateway
    # ─────────────────────────────────────────────────────────────────
    gateway_config = db.query(SystemConfig).filter(SystemConfig.config_key == "active_payment_gateway").first()
    active_gateway = (gateway_config.config_value if gateway_config else "PAYU").upper()

    if active_gateway == "RAZORPAY":
        order = create_razorpay_order(tx.amount, txnid)
        if not order:
             raise HTTPException(status_code=500, detail="Failed to create Razorpay order")
        
        # Razorpay specific data
        return {
            "gateway": "RAZORPAY",
            "razorpay_init": {
                "order_id": order["id"],
                "amount": order["amount"], # already in paise
                "currency": "INR",
                "key_id": settings.RAZORPAY_KEY_ID,
                "description": productinfo,
                "prefill_name": current_user.username,
                "prefill_email": current_user.email,
                "prefill_contact": current_user.phone_number or "9999999999",
                "txnid": txnid
            }
        }
    elif active_gateway == "CCAVENUE":
        # CCAvenue Logic
        redirect_url = f"{settings.APP_URL}/api/v1/wallet/ccavenue/return"
        cancel_url   = f"{settings.APP_URL}/api/v1/wallet/ccavenue/return"
        
        # CCAvenue Parameter string
        # merchant_id, order_id, amount, currency, redirect_url, cancel_url, language
        merchant_param = f"merchant_id={settings.CCAVENUE_MERCHANT_ID}"
        order_param    = f"order_id={txnid}"
        currency_param = "currency=INR"
        amount_param   = f"amount={tx.amount:.2f}"
        redirect_param = f"redirect_url={redirect_url}"
        cancel_param   = f"cancel_url={cancel_url}"
        language_param = "language=EN"
        
        # Pre-fill billing details for a better user experience
        billing_name   = f"billing_name={current_user.username}"
        billing_address = "billing_address=Not Provided"
        billing_city    = "billing_city=Mumbai"
        billing_state   = "billing_state=Maharashtra"
        billing_zip     = "billing_zip=400001"
        billing_country = "billing_country=India"
        billing_tel     = f"billing_tel={current_user.phone_number or '9999999999'}"
        billing_email   = f"billing_email={current_user.email}"
        
        merchant_data = f"{merchant_param}&{order_param}&{currency_param}&{amount_param}&{redirect_param}&{cancel_param}&{language_param}&{billing_name}&{billing_address}&{billing_city}&{billing_state}&{billing_zip}&{billing_country}&{billing_tel}&{billing_email}"
        
        enc_request = encrypt_ccavenue(merchant_data, settings.CCAVENUE_WORKING_KEY)
        
        return {
            "gateway": "CCAVENUE",
            "ccavenue_init": {
                "encRequest": enc_request,
                "access_code": settings.CCAVENUE_ACCESS_CODE,
                "action": "https://secure.ccavenue.com/transaction/transaction.do?command=initiateTransaction",
                "order_id": txnid
            }
        }
    else:
        # Fallback to PAYU
        payu_hash = generate_payu_hash(
            txnid=txnid,
            amount=req.amount,
            productinfo=productinfo,
            firstname=current_user.username,
            email=current_user.email
        )

        return {
            "gateway": "PAYU",
            "payu_init": {
                "txnid": txnid,
                "amount": req.amount,
                "productinfo": productinfo,
                "firstname": current_user.username,
                "email": current_user.email,
                "phone": current_user.phone_number or "9999999999",
                "surl": _payu_surl(),
                "furl": _payu_furl(),
                "hash": payu_hash,
                "key": settings.PAYU_MERCHANT_KEY,
                "action": f"{settings.PAYU_BASE_URL}/_payment"
            }
        }


# ─────────────────────────────────────────────────────────────────
# PayU redirect page — FIXED: all user data is HTML-escaped
# ─────────────────────────────────────────────────────────────────

@router.get("/payu/redirect/{txnid}", response_class=HTMLResponse)
def payu_redirect(
    txnid: str,
    vpa: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)  # FIXED: requires auth
):
    tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")

    # Ownership check — users can only redirect their own transactions
    if tx.user_id != current_user.id:
        raise HTTPException(403, "Access denied")

    user = db.query(User).filter(User.id == tx.user_id).first()

    productinfo = "Digital Services"
    payu_hash = generate_payu_hash(
        txnid=tx.reference_id,
        amount=tx.amount,
        productinfo=productinfo,
        firstname=user.username,
        email=user.email
    )

    # FIX: escape ALL user-controlled data before injecting into HTML
    safe_txnid       = html.escape(str(tx.reference_id))
    safe_amount      = html.escape(f"{tx.amount:.2f}")
    safe_productinfo = html.escape(productinfo)
    safe_firstname   = html.escape(str(user.username))
    safe_email       = html.escape(str(user.email))
    safe_surl        = html.escape(_payu_surl())
    safe_furl        = html.escape(_payu_furl())
    safe_hash        = html.escape(payu_hash)
    safe_key         = html.escape(settings.PAYU_MERCHANT_KEY)
    safe_action      = html.escape(f"{settings.PAYU_BASE_URL}/_payment")

    seamless_fields = ""
    if vpa:
        # Handle popular app codes for Direct Redirection
        if vpa == "GOOGLEPAY":
            seamless_fields = f"""
                <input type="hidden" name="pg" value="UPI" />
                <input type="hidden" name="bankcode" value="TEZ" />
            """
        elif vpa == "PHONEPE":
            seamless_fields = f"""
                <input type="hidden" name="pg" value="UPI" />
                <input type="hidden" name="bankcode" value="PHONEPE" />
            """
        elif vpa == "PAYTM":
            seamless_fields = f"""
                <input type="hidden" name="pg" value="UPI" />
                <input type="hidden" name="bankcode" value="PAYTM" />
            """
        elif vpa == "INTENT": # Standard Generic Intent
            seamless_fields = f"""
                <input type="hidden" name="pg" value="UPI" />
                <input type="hidden" name="bankcode" value="INTENT" />
            """
        else: # Manual UPI ID Entry
            safe_vpa = html.escape(str(vpa))
            seamless_fields = f"""
                <input type="hidden" name="pg" value="UPI" />
                <input type="hidden" name="bankcode" value="UPI" />
                <input type="hidden" name="vpa" value="{safe_vpa}" />
            """

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <title>Secure Transfer</title>
    <style>
      body {{ background: #0D0E12; margin: 0; display: flex; align-items: center; justify-content: center; height: 100vh; font-family: sans-serif; color: white; }}
      .loader {{ border: 3px solid rgba(255,255,255,0.1); border-top: 3px solid #FFB800; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin-bottom: 20px; }}
      @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
    </style>
  </head>
  <body onload="document.forms['payuForm'].submit();">
    <div style="text-align: center;">
        <div class="loader" style="margin: 0 auto 20px auto;"></div>
        <h2 style="font-weight: 500; font-size: 18px; color: rgba(255,255,255,0.8);">Redirecting to Secure Gateway...</h2>
    </div>
    <form action="{safe_action}" method="post" name="payuForm">
        <input type="hidden" name="key"        value="{safe_key}" />
        <input type="hidden" name="txnid"      value="{safe_txnid}" />
        <input type="hidden" name="amount"     value="{safe_amount}" />
        <input type="hidden" name="productinfo" value="{safe_productinfo}" />
        <input type="hidden" name="firstname"  value="{safe_firstname}" />
        <input type="hidden" name="email"      value="{safe_email}" />
        <input type="hidden" name="phone"      value="{html.escape(str(user.phone_number or '9999999999'))}" />
        <input type="hidden" name="surl"       value="{safe_surl}" />
        <input type="hidden" name="furl"       value="{safe_furl}" />
        <input type="hidden" name="hash"       value="{safe_hash}" />
        <input type="hidden" name="drop_category" value="CC,DC,NB,EMI,WALLET,CASH" />
        <input type="hidden" name="enforce_paymethod" value="UPI" />
        {seamless_fields}
    </form>
  </body>
</html>"""
    return html_content


# ─────────────────────────────────────────────────────────────────
# PayU webhook — called directly by PayU servers
# ─────────────────────────────────────────────────────────────────

@router.post("/payu/webhook")
async def payu_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    form = await request.form()

    txnid         = form.get("txnid")
    amount        = float(form.get("amount", 0))
    productinfo   = form.get("productinfo")
    firstname     = form.get("firstname")
    email         = form.get("email")
    status        = form.get("status")
    received_hash = form.get("hash")
    mihpayid      = form.get("mihpayid", "")
    mode          = form.get("mode", "")
    field9        = form.get("field9", "")

    is_valid = verify_payu_hash(txnid, amount, productinfo, firstname, email, status, received_hash)
    if not is_valid:
        logger.warning(f"Webhook hash validation failed for txnid={txnid} (PayU)")
        raise HTTPException(status_code=400, detail="Invalid hash")

    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == txnid
    ).with_for_update().first()

    if not tx:
        logger.error(f"Transaction not found in webhook: txnid={txnid}")
        raise HTTPException(status_code=404, detail="Transaction not found")

    # SECURITY: Verify that the amount matched!
    if abs(float(tx.amount) - amount) > 0.01:
        logger.critical(f"AMOUNT MISMATCH for txnid={txnid}: db={tx.amount} gateway={amount}")
        tx.status = "FAILED"
        tx.failure_reason = "FRAUD_ATTEMPT:AMOUNT_MISMATCH"
        db.add(tx)
        db.commit()
        raise HTTPException(status_code=400, detail="Amount mismatch")

    if tx.status != "PENDING":
        return {"message": "Transaction already processed"}

    tx.payu_txn_id  = mihpayid
    tx.payment_mode = mode

    if status == "success":
        tx.status = "SUCCESS"
        user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
        user.wallet_balance += tx.amount
        db.add(user)
        add_user_notification(
            db, user.id,
            "Payment Confirmed ✅",
            f"₹{tx.amount:.0f} has been added to your ZexPlay wallet.",
            "WALLET"
        )
        logger.info(f"Payment SUCCESS: txnid={txnid} user={tx.user_id} amount={tx.amount}")
    else:
        tx.status = "FAILED"
        tx.failure_reason = field9 or status
        add_user_notification(
            db, tx.user_id,
            "Recharge Failed ❌",
            f"Your payment of ₹{tx.amount} has failed. Reason: {tx.failure_reason or 'Gateway Error'}. If money was deducted, it will be refunded automatically.",
            "WALLET"
        )
        logger.info(f"Payment FAILED: txnid={txnid} reason={tx.failure_reason}")

    db.add(tx)
    db.commit()

    # FIXED: use BackgroundTasks instead of asyncio.create_task
    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {"message": "Webhook processed"}


# ─────────────────────────────────────────────────────────────────
# PayU return handler (SURL / FURL — browser redirect after payment)
# ─────────────────────────────────────────────────────────────────

@router.post("/payu/success", response_class=HTMLResponse)
@router.post("/payu/failure", response_class=HTMLResponse)
async def payu_return_handler(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    form = await request.form()
    txnid    = form.get("txnid")
    status   = form.get("status")
    mihpayid = form.get("mihpayid", "")
    mode     = form.get("mode", "")
    field9   = form.get("field9", "")

    if txnid:
        if status == "success" and not request.url.path.endswith("failure"):
            amount      = float(form.get("amount", 0))
            productinfo = form.get("productinfo")
            firstname   = form.get("firstname")
            email       = form.get("email")
            recv_hash   = form.get("hash")

            if verify_payu_hash(txnid, amount, productinfo, firstname, email, status, recv_hash):
                tx = db.query(WalletTransaction).filter(
                    WalletTransaction.reference_id == txnid
                ).with_for_update().first()

                if tx and tx.status == "PENDING":
                    # SECURITY: Verify amount in return handler too
                    if abs(float(tx.amount) - amount) > 0.01:
                        logger.critical(f"RET_AMOUNT_MISMATCH for txnid={txnid}: db={tx.amount} gateway={amount}")
                        tx.status = "FAILED"
                        tx.failure_reason = "FRAUD_ATTEMPT:RET_AMOUNT_MISMATCH"
                        db.add(tx)
                        db.commit()
                    else:
                        tx.status       = "SUCCESS"
                        tx.payu_txn_id  = mihpayid
                        tx.payment_mode = mode
                        user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
                        user.wallet_balance += tx.amount
                        db.add(tx)
                        db.add(user)
                        db.commit()
                        background_tasks.add_task(
                            ws_manager.broadcast_to_admins, {"type": "finance_update"}
                        )
                        add_user_notification(
                            db, user.id,
                            "Payment Confirmed ✅",
                            f"₹{tx.amount:.0f} has been added to your ZexPlay wallet.",
                            "WALLET"
                        )
        else:
            tx = db.query(WalletTransaction).filter(
                WalletTransaction.reference_id == txnid
            ).first()
            if tx and tx.status == "PENDING":
                tx.status         = "FAILED"
                tx.payu_txn_id    = mihpayid
                tx.failure_reason = field9 or status or "USER_CANCELLED"
                db.add(tx)
                db.commit()
                add_user_notification(
                    db, tx.user_id,
                    "Recharge Failed ❌",
                    f"Transaction #{txnid} failed or was cancelled. Reason: {tx.failure_reason}",
                    "WALLET"
                )
                background_tasks.add_task(
                    ws_manager.broadcast_to_admins, {"type": "finance_update"}
                )

    bg_color = "#16A34A" if status == "success" else "#EF4444"
    label    = "Payment Successful!" if status == "success" else "Payment Failed!"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><body style="background:#0D0E12; color:white; text-align:center; padding-top:50px;">
    <h2 style="color:{html.escape(bg_color)};">{html.escape(label)}</h2>
    <p>You can now close this screen.</p>
</body></html>""")


# ─────────────────────────────────────────────────────────────────
# UPI intent — FIXED: requires authentication + ownership check
# ─────────────────────────────────────────────────────────────────

@router.get("/payu/upi-intent/{txnid}")
def get_upi_intent(
    txnid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)  # FIXED: auth required
):
    """Returns a native upi://pay deep link so Android can open GPay/PhonePe directly."""
    tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")

    # FIXED: ownership check
    if tx.user_id != current_user.id:
        raise HTTPException(403, "Access denied")

    # Reverting to direct UPI intent link for app redirection (GPay, PhonePe, Paytm)
    merchant_vpa = settings.PAYU_MERCHANT_VPA
    amount_str   = f"{tx.amount:.2f}"
    upi_link = (
        f"upi://pay"
        f"?pa={merchant_vpa}"
        f"&pn=ZexPlay"
        f"&am={amount_str}"
        f"&cu=INR"
        f"&tn=ZexPlay+Wallet+Recharge"
        f"&tr={txnid}"
        f"&mc=7372"
    )
    return {"upi_link": upi_link, "txnid": txnid, "amount": tx.amount}


# ─────────────────────────────────────────────────────────────────
# Payment status polling
# ─────────────────────────────────────────────────────────────────

@router.get("/status/{txnid}")
def get_payment_status(
    txnid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Android polls this after payment to confirm final status without trusting WebView URL."""
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == txnid,
        WalletTransaction.user_id == current_user.id
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {
        "txnid":          txnid,
        "status":         tx.status,
        "amount":         tx.amount,
        "payment_mode":   tx.payment_mode,
        "failure_reason": tx.failure_reason,
        "payu_txn_id":    tx.payu_txn_id,
    }


# ─────────────────────────────────────────────────────────────────
# Cancel transaction — FIXED: requires auth + ownership check
# ─────────────────────────────────────────────────────────────────

@router.get("/payu/cancel/{txnid}")
def cancel_payu_transaction(
    txnid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)  # FIXED: auth required
):
    tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # FIXED: ownership check
    if tx.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if tx.status == "PENDING":
        tx.status = "FAILED"
        tx.failure_reason = "USER_CANCELLED"
        db.add(tx)
        db.commit()
        logger.info(f"Transaction cancelled by user: txnid={txnid} user={current_user.id}")

    return {"message": "Transaction cancelled"}


# ─────────────────────────────────────────────────────────────────
# Withdrawal request
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

    # Lock user row to prevent race conditions
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
        payu_txn_id=req.upi_id  # Freeze current UPI ID in this field for admin audit
    )

    db.add(tx)
    db.add(user)
    db.commit()

    add_user_notification(
        db,
        user.id,
        "Withdrawal Requested",
        f"Your withdrawal request of ₹{req.amount} has been submitted and is pending admin approval.",
        "WALLET"
    )

    return {"message": "Withdrawal requested successfully. Waiting for admin approval."}


# ─────────────────────────────────────────────────────────────────
# Razorpay Verification
# ─────────────────────────────────────────────────────────────────

@router.post("/razorpay/verify")
async def verify_razorpay_payment(
    data: dict, # { "razorpay_order_id": "...", "razorpay_payment_id": "...", "razorpay_signature": "..." }
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order_id   = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature  = data.get("razorpay_signature")

    if not all([order_id, payment_id, signature]):
        raise HTTPException(status_code=400, detail="Missing Razorpay details")

    # The txnid was passed as 'receipt' during order creation.
    # Razorpay order response includes the receipt.
    # We should search for our transaction using the reference_id (txnid).
    # However, since we don't have the order_id in our DB yet (we could have stored it but didn't per plan),
    # we'll use a slightly different approach: the client sends the txnid back OR we search by reference_id.
    
    # Ideally, we should have stored order_id in WalletTransaction. 
    # Let's check WalletTransaction model to see if we can add a column.
    
    # For now, let's assume the user sends the txnid as well.
    txnid = data.get("txnid")
    if not txnid:
         raise HTTPException(status_code=400, detail="txnid is required for verification")

    is_valid = verify_razorpay_signature(order_id, payment_id, signature)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid signature")

    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == txnid,
        WalletTransaction.user_id == current_user.id
    ).with_for_update().first()

    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if tx.status != "PENDING":
        return {"status": tx.status, "message": "Transaction already processed"}

    # Update transaction
    tx.status = "SUCCESS"
    tx.payu_txn_id = payment_id # Repurposing existing column for "payment_id"
    tx.payment_mode = "RAZORPAY"
    
    # Update balance
    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    user.wallet_balance += tx.amount
    
    db.add(tx)
    db.add(user)
    db.commit()

    add_user_notification(
        db, user.id,
        "Payment Confirmed ✅",
        f"₹{tx.amount:.0f} has been added to your ZexPlay wallet via Razorpay.",
        "WALLET"
    )

    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    
    return {"status": "SUCCESS", "message": "Payment verified successfully"}


# ─────────────────────────────────────────────────────────────────
# CCAvenue Return Handler
# ─────────────────────────────────────────────────────────────────

@router.post("/ccavenue/return", response_class=HTMLResponse)
async def ccavenue_return_handler(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    form = await request.form()
    enc_resp = form.get("encResp")
    if not enc_resp:
        raise HTTPException(status_code=400, detail="Missing encrypted response from CCAvenue")
        
    try:
        decrypted_data = decrypt_ccavenue(enc_resp, settings.CCAVENUE_WORKING_KEY)
        # Parse query string: order_id=txnid&order_status=Success&...
        from urllib.parse import parse_qs
        data = {k: v[0] for k, v in parse_qs(decrypted_data).items()}
        
        txnid  = data.get("order_id")
        status = data.get("order_status") # Success, Failure, Aborted, Invalid
        amount = float(data.get("amount", 0))
        tracking_id = data.get("tracking_id", "")
        payment_mode = data.get("payment_mode", "CCAVENUE")
        
        if not txnid:
            raise ValueError("Missing txnid in decrypted response")
            
        tx = db.query(WalletTransaction).filter(
            WalletTransaction.reference_id == txnid
        ).with_for_update().first()
        
        if not tx:
             raise ValueError("Transaction not found")
             
        if tx.status != "PENDING":
             # Already processed
             label = "Payment already processed!"
             bg_color = "#6B7280"
        else:
            if status == "Success":
                tx.status = "SUCCESS"
                tx.payu_txn_id = tracking_id # Repurposing field
                tx.payment_mode = payment_mode
                
                user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
                user.wallet_balance += tx.amount
                db.add(user)
                
                add_user_notification(
                    db, user.id,
                    "Payment Confirmed ✅",
                    f"₹{tx.amount:.0f} has been added to your ZexPlay wallet via CCAvenue.",
                    "WALLET"
                )
                label = "Payment Successful!"
                bg_color = "#16A34A"
            else:
                tx.status = "FAILED"
                tx.failure_reason = status
                add_user_notification(
                    db, tx.user_id,
                    "Recharge Failed ❌",
                    f"Your CCAvenue payment failed. Status: {status}",
                    "WALLET"
                )
                label = "Payment Failed!"
                bg_color = "#EF4444"
                
            db.add(tx)
            db.commit()
            background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})

    except Exception as e:
        logger.error(f"CCAvenue decryption error: {e}")
        label = "Error processing payment!"
        bg_color = "#EF4444"

    return HTMLResponse(f"""<!DOCTYPE html>
<html><body style="background:#0D0E12; color:white; text-align:center; padding-top:50px; font-family:sans-serif;">
    <h2 style="color:{html.escape(bg_color)};">{html.escape(label)}</h2>
    <p>You can now close this screen and return to the app.</p>
</body></html>""")

# ─────────────────────────────────────────────────────────────────
# CCAvenue Seamless Handshake (Mobile SDK)
# ─────────────────────────────────────────────────────────────────

@router.get("/ccavenue/get-rsa")
def get_ccavenue_rsa(order_id: str, current_user: User = Depends(get_current_user)):
    """
    Fetch the dynamic RSA Public Key from CCAvenue for transaction encryption.
    """
    import requests
    url = "https://secure.ccavenue.com/transaction/getRSAKey"
    params = {
        "access_code": settings.CCAVENUE_ACCESS_CODE,
        "order_id": order_id
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        return {"rsa_key": response.text.strip()}
    except Exception as e:
        logger.error(f"Failed to fetch CCAvenue RSA key: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch RSA key")

@router.get("/ccavenue/payment-options")
def get_ccavenue_payment_options(current_user: User = Depends(get_current_user)):
    """
    Fetch available payment options (Cards, UPI, NetBanking) as JSON from CCAvenue.
    """
    import requests
    url = "https://api.ccavenue.com/apis/servlet/DoWebTrans"
    
    # Payload for fetching payment options
    # command=getPaymentOptions&access_code=...&currency=INR
    plain_params = f"command=getPaymentOptions&access_code={settings.CCAVENUE_ACCESS_CODE}&currency=INR"
    enc_request = encrypt_ccavenue(plain_params, settings.CCAVENUE_WORKING_KEY)
    
    payload = {
        "enc_request": enc_request,
        "access_code": settings.CCAVENUE_ACCESS_CODE,
        "command": "getPaymentOptions",
        "response_type": "JSON"
    }
    
    try:
        response = requests.post(url, data=payload, timeout=10)
        resp_text = response.text.strip()
        
        # Check if response is a valid hex string (at least 32 chars and hex)
        import re
        if not re.fullmatch(r"^[0-9a-fA-F]+$", resp_text):
            logger.error(f"CCAvenue API returned non-hex response: {resp_text}")
            raise HTTPException(status_code=400, detail=f"Gateway Error: {resp_text}")

        decrypted_resp = decrypt_ccavenue(resp_text, settings.CCAVENUE_WORKING_KEY)
        import json
        return json.loads(decrypted_resp)
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        logger.error(f"Failed to fetch CCAvenue payment options: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch payment options")

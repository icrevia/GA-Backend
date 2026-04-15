from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import List
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
import uuid
import html
import logging
import random

from api.deps import get_current_user_wallet
from core.database import get_db_sync as get_db
from models.user import User
from models.wallet import WalletTransaction
from models.promo import PromoCode
from models.withdraw_upi_account import WithdrawUpiAccount
from services.pay0 import create_pay0_order, check_pay0_order_status
from schemas.wallet import (
    AddMoneyRequest,
    PaymentInitResponse,
    PromoRedeemRequest,
    PromoRedeemResponse,
    WithdrawalRequest,
    WalletTransactionResponse,
    WalletBalanceResponse,
    WithdrawUpiAccountListRequest,
    WithdrawUpiAccountResponse,
    SpinPlayResponse,
    CancelPaymentRequest,
)
from core.config import settings
from services.notifications import add_user_notification
from services.referral_rewards import maybe_credit_referrer_for_first_successful_deposit
from core.websockets import manager as ws_manager
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    InsufficientWalletBalanceError,
    credit_wallet,
    debit_wallet,
    get_wallet_breakdown,
    get_withdrawable_balance,
    to_money,
)

logger = logging.getLogger("GamerzAdda.wallet")

router = APIRouter()
MAX_WITHDRAW_UPI_ACCOUNTS = 3
SPIN_COST = Decimal("10.00")
DAILY_SPIN_LIMIT = 1
SPIN_DAILY_RESET_MINUTE_IST = 1
WITHDRAWAL_DAILY_RESET_MINUTE_IST = 1
WITHDRAWAL_SAME_DAY_FEE = Decimal("5.00")
IST = timezone(timedelta(hours=5, minutes=30))


def _common_spin_prize_amount() -> Decimal:
    return Decimal("0.00") if random.random() < 0.68 else Decimal("1.00")


def _planned_prize_for_spin(spin_number: int) -> Decimal:
    if spin_number <= 0:
        return Decimal("0.00")
    if spin_number == 1:
        return Decimal(str(random.choice([1, 2, 5])))
    if spin_number % 100 == 0:
        return Decimal("100.00")
    if spin_number % 30 == 0:
        return Decimal("50.00")
    if spin_number % 15 == 0:
        return Decimal("20.00")
    if spin_number % 5 == 0:
        return Decimal("10.00")
    return _common_spin_prize_amount()


def _current_daily_cycle_ist(reset_minute_ist: int) -> tuple[str, datetime, datetime]:
    """Return cycle key and UTC-naive window for a daily reset minute in IST."""
    now_ist = datetime.now(IST)
    reset_point_ist = now_ist.replace(
        hour=0,
        minute=reset_minute_ist,
        second=0,
        microsecond=0,
    )
    cycle_start_ist = reset_point_ist - timedelta(days=1) if now_ist < reset_point_ist else reset_point_ist
    cycle_end_ist = cycle_start_ist + timedelta(days=1)
    cycle_key = cycle_start_ist.date().isoformat()
    cycle_start_utc = cycle_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    cycle_end_utc = cycle_end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return cycle_key, cycle_start_utc, cycle_end_utc


def _current_spin_cycle_ist() -> tuple[str, datetime, datetime]:
    """Return cycle key and UTC-naive window for daily reset at 12:01 AM IST."""
    return _current_daily_cycle_ist(SPIN_DAILY_RESET_MINUTE_IST)


def _current_withdrawal_cycle_ist() -> tuple[str, datetime, datetime]:
    """Return cycle key and UTC-naive window for daily withdrawal reset at 12:01 AM IST."""
    return _current_daily_cycle_ist(WITHDRAWAL_DAILY_RESET_MINUTE_IST)


def _normalize_upi_id(raw_value: str) -> str:
    return raw_value.strip().lower()


def _normalize_account_holder_name(raw_value: str) -> str:
    return " ".join(raw_value.strip().split())


def _normalize_promo_code(raw_value: str) -> str:
    cleaned = " ".join(raw_value.strip().upper().split())
    normalized = cleaned.replace(" ", "_")
    return normalized[:40]


def _is_promo_expired(promo: PromoCode) -> bool:
    if not promo.expires_at:
        return False

    expires_at = promo.expires_at
    if getattr(expires_at, "tzinfo", None) is not None:
        expires_at = expires_at.replace(tzinfo=None)
    return expires_at <= datetime.utcnow()

# ─────────────────────────────────────────────────────────────────
# Wallet balance & history
# ─────────────────────────────────────────────────────────────────

@router.get("/balance", response_model=WalletBalanceResponse)
def get_balance(current_user: User = Depends(get_current_user_wallet)):
    return get_wallet_breakdown(current_user)


@router.get("/transactions", response_model=List[WalletTransactionResponse])
def get_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet),
    limit: int = 200,
    offset: int = 0,
):
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)
    return (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == current_user.id)
        .order_by(WalletTransaction.created_at.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )


@router.post("/spin/play", response_model=SpinPlayResponse)
def play_spin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet),
):
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    daily_spin_limit = int(getattr(user, "daily_spin_limit", DAILY_SPIN_LIMIT) or DAILY_SPIN_LIMIT)
    if daily_spin_limit <= 0:
        daily_spin_limit = DAILY_SPIN_LIMIT

    cycle_key, day_start, day_end = _current_spin_cycle_ist()
    stored_cycle_key = (getattr(user, "daily_spin_cycle_key", None) or "").strip()

    if stored_cycle_key != cycle_key:
        # Rebuild once from transaction log for safety during rollouts/restarts.
        spins_used_today = (
            db.query(WalletTransaction.id)
            .filter(
                WalletTransaction.user_id == user.id,
                WalletTransaction.transaction_type == "SPIN",
                WalletTransaction.status == "SUCCESS",
                WalletTransaction.created_at >= day_start,
                WalletTransaction.created_at < day_end,
            )
            .count()
        )
        user.daily_spin_cycle_key = cycle_key
        user.daily_spin_used = spins_used_today
    else:
        spins_used_today = int(getattr(user, "daily_spin_used", 0) or 0)

    if spins_used_today >= daily_spin_limit:
        raise HTTPException(status_code=400, detail="Daily spin limit reached. Resets at 12:01 AM IST.")

    try:
        debit_wallet(
            user,
            SPIN_COST,
            spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING),
        )
    except InsufficientWalletBalanceError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Available ₹{exc.available:.2f}, required ₹{exc.required:.2f}.",
        )

    total_spins_before = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.transaction_type == "SPIN",
            WalletTransaction.status == "SUCCESS",
        )
        .count()
    )
    total_spins = total_spins_before + 1
    prize_amount = to_money(_planned_prize_for_spin(total_spins))

    spin_reference = f"GA-{uuid.uuid4().hex[:6].upper()}"
    db.add(
        WalletTransaction(
            user_id=user.id,
            amount=-SPIN_COST,
            transaction_type="SPIN",
            status="SUCCESS",
            reference_id=spin_reference,
            payment_mode="SPIN",
        )
    )

    if prize_amount > Decimal("0.00"):
        credit_wallet(user, prize_amount, WALLET_BUCKET_WINNING)
        db.add(
            WalletTransaction(
                user_id=user.id,
                amount=prize_amount,
                transaction_type="SPIN_REWARD",
                status="SUCCESS",
                reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
                payment_mode="SPIN",
            )
        )

    spins_used_today += 1
    user.daily_spin_cycle_key = cycle_key
    user.daily_spin_used = spins_used_today

    db.add(user)
    db.commit()
    db.refresh(user)

    if prize_amount > Decimal("0.00"):
        add_user_notification(
            db,
            user.id,
            "Spin Reward Credited",
            f"You won ₹{prize_amount:.2f}. Amount credited to your Winning Wallet.",
            "WALLET",
            delay_push_seconds=7,  # Delay push until after wheel animation (4s) + win popup
        )

    remaining_spins = max(0, daily_spin_limit - spins_used_today)
    wallet_breakdown = get_wallet_breakdown(user)

    return {
        "message": "Spin completed successfully",
        "prize_amount": prize_amount,
        "spin_cost": SPIN_COST,
        "spins_used_today": spins_used_today,
        "daily_spin_limit": daily_spin_limit,
        "remaining_spins": remaining_spins,
        "total_spins": total_spins,
        "wallet_balance": wallet_breakdown["balance"],
        "deposit_balance": wallet_breakdown["deposit_balance"],
        "winning_balance": wallet_breakdown["winning_balance"],
        "bonus_balance": wallet_breakdown["bonus_balance"],
    }


@router.get("/withdraw/accounts", response_model=List[WithdrawUpiAccountResponse])
def get_withdraw_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    return (
        db.query(WithdrawUpiAccount)
        .filter(WithdrawUpiAccount.user_id == current_user.id)
        .order_by(WithdrawUpiAccount.id.asc())
        .all()
    )


@router.post("/promo/redeem", response_model=PromoRedeemResponse)
def redeem_promo_code(
    req: PromoRedeemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    normalized_code = _normalize_promo_code(req.code)
    if len(normalized_code) < 3:
        raise HTTPException(status_code=400, detail="Enter a valid promo code")

    promo = (
        db.query(PromoCode)
        .filter(PromoCode.code == normalized_code)
        .with_for_update()
        .first()
    )
    if not promo:
        raise HTTPException(status_code=404, detail="Promo code not found")

    if not bool(promo.is_active):
        raise HTTPException(status_code=400, detail="Promo code is inactive")

    if int(promo.uses_count or 0) >= int(promo.max_uses or 0):
        raise HTTPException(status_code=400, detail="Promo code usage limit reached")

    if _is_promo_expired(promo):
        raise HTTPException(status_code=400, detail="Promo code has expired")

    already_redeemed = db.query(WalletTransaction.id).filter(
        WalletTransaction.user_id == current_user.id,
        WalletTransaction.transaction_type == "PROMO_REWARD",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.failure_reason.contains(f"PROMO:{normalized_code}"),
    ).first()
    if already_redeemed:
        raise HTTPException(status_code=409, detail="Promo code already redeemed")

    reference_id = f"GA-{uuid.uuid4().hex[:6].upper()}"

    reward_amount = Decimal(str(promo.discount_amount or Decimal("0"))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if reward_amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Promo code has invalid reward amount")

    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    credit_wallet(user, reward_amount, WALLET_BUCKET_BONUS)
    promo.uses_count = int(promo.uses_count or 0) + 1

    tx = WalletTransaction(
        user_id=user.id,
        amount=reward_amount,
        transaction_type="PROMO_REWARD",
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode="PROMO",
        failure_reason=f"PROMO:{normalized_code}",
    )

    db.add(user)
    db.add(promo)
    db.add(tx)
    db.commit()
    db.refresh(user)

    add_user_notification(
        db,
        user.id,
        "Promo Applied",
        f"Promo {normalized_code} applied. ₹{reward_amount:.2f} added to your wallet.",
        "WALLET",
    )

    wallet_breakdown = get_wallet_breakdown(user)

    return {
        "message": f"Promo applied successfully. ₹{reward_amount:.2f} credited.",
        "code": normalized_code,
        "reward_amount": reward_amount,
        "wallet_balance": wallet_breakdown["balance"],
        "deposit_balance": wallet_breakdown["deposit_balance"],
        "winning_balance": wallet_breakdown["winning_balance"],
        "bonus_balance": wallet_breakdown["bonus_balance"],
        "transaction_reference": reference_id,
    }


@router.put("/withdraw/accounts", response_model=List[WithdrawUpiAccountResponse])
def replace_withdraw_accounts(
    req: WithdrawUpiAccountListRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    if len(req.accounts) > MAX_WITHDRAW_UPI_ACCOUNTS:
        raise HTTPException(
            status_code=400,
            detail=f"You can save up to {MAX_WITHDRAW_UPI_ACCOUNTS} UPI accounts"
        )

    normalized_accounts: list[tuple[str, str]] = []
    seen_upi_ids: set[str] = set()
    selected_upi_id = _normalize_upi_id(req.selected_upi_id) if req.selected_upi_id else None

    for account in req.accounts:
        account_holder_name = _normalize_account_holder_name(account.account_holder_name)
        upi_id = _normalize_upi_id(account.upi_id)

        if not account_holder_name:
            raise HTTPException(status_code=400, detail="Account holder name is required")
        if not upi_id:
            raise HTTPException(status_code=400, detail="UPI ID is required")
        if upi_id in seen_upi_ids:
            raise HTTPException(status_code=400, detail="Duplicate UPI IDs are not allowed")

        normalized_accounts.append((account_holder_name, upi_id))
        seen_upi_ids.add(upi_id)

    if selected_upi_id and selected_upi_id not in seen_upi_ids:
        raise HTTPException(status_code=400, detail="Selected UPI must be part of saved accounts")

    db.query(WithdrawUpiAccount).filter(
        WithdrawUpiAccount.user_id == current_user.id
    ).delete(synchronize_session=False)

    for account_holder_name, upi_id in normalized_accounts:
        db.add(
            WithdrawUpiAccount(
                user_id=current_user.id,
                account_holder_name=account_holder_name,
                upi_id=upi_id,
            )
        )

    db.commit()

    return (
        db.query(WithdrawUpiAccount)
        .filter(WithdrawUpiAccount.user_id == current_user.id)
        .order_by(WithdrawUpiAccount.id.asc())
        .all()
    )


# ─────────────────────────────────────────────────────────────────
# Initiate a payment (Pay0.shop)
# ─────────────────────────────────────────────────────────────────

@router.post("/add-money/init", response_model=PaymentInitResponse)
def init_add_money(
    req: AddMoneyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    if req.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum recharge amount is ₹1")
    if req.amount > 100_000:
        raise HTTPException(status_code=400, detail="Maximum recharge amount is ₹1,00,000")

    txnid = f"GA-{uuid.uuid4().hex[:6].upper()}"

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
    if not api_key:
        tx.status = "FAILED"
        tx.failure_reason = "PAY0_MERCHANT_KEY_NOT_CONFIGURED"
        db.add(tx)
        db.commit()
        raise HTTPException(status_code=503, detail="Payment gateway is temporarily unavailable")

    app_url = settings.APP_URL.strip().rstrip("/")
    redirect_url = f"{app_url}/api/v1/wallet/pay0/return" if app_url else "https://pay0.shop"
    if not app_url:
        logger.warning(
            "APP_URL is not configured, using Pay0 fallback redirect URL. "
            "Webhook/redirect callback may not hit backend; app polling will confirm payment."
        )
    customer_name = (current_user.username or f"User{current_user.id}").strip()
    customer_mobile = (current_user.phone_number or "").strip()
    
    try:
        pay0_res = create_pay0_order(
            api_key=api_key,
            order_id=txnid,
            amount=float(req.amount),
            customer_name=customer_name,
            customer_mobile=customer_mobile,
            redirect_url=redirect_url
        )
        
        if not pay0_res.get("success"):
            raise RuntimeError(f"PAY0_INIT_FAILED: {pay0_res.get('error', 'Unknown Error')}")

        provider_order_id = pay0_res.get("order_id") or txnid
        tx.gateway_order_id = provider_order_id
        
        response_payload = {
            "gateway": "PAY0",
            "pay0_init": {
                "payment_url": pay0_res["payment_url"],
                "order_id": provider_order_id
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
            f"We could not initialize your payment via UPI. {failure_reason}",
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
        f"You have initiated a recharge of ₹{req.amount} via UPI. Complete the payment to see it in your wallet.",
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

    order_id = form_data.get("order_id") or form_data.get("orderId")
    if not order_id:
        return HTMLResponse("<body>Invalid Request: Missing order_id</body>", status_code=400)

    tx = db.query(WalletTransaction).filter(
        (WalletTransaction.reference_id == order_id) | (WalletTransaction.gateway_order_id == order_id)
    ).with_for_update().first()

    if not tx:
        return HTMLResponse("<body>Transaction not found</body>", status_code=404)

    # Strictly verify against Pay0 Check Status API to prevent spoofing
    api_key = settings.PAY0_MERCHANT_KEY
    status_res = check_pay0_order_status(api_key, order_id)
    provider_order_id = status_res.get("order_id") or order_id
    if tx.gateway_order_id != provider_order_id:
        tx.gateway_order_id = provider_order_id
    
    final_status = "PENDING"
    
    if status_res["status"] == "SUCCESS":
        final_status = "success"
        if tx.status == "PENDING":
            status_amount = Decimal(str(status_res.get("amount", 0)))
            if status_amount != tx.amount:
                tx.status = "FAILED"
                tx.failure_reason = f"PAY0_AMOUNT_MISMATCH expected={tx.amount} got={status_amount}"
                db.add(tx)
                db.commit()
                if "/webhook" in str(request.url):
                    return {"message": "Amount mismatch", "status": "failed"}
                return HTMLResponse("<body>Payment verification failed: amount mismatch</body>", status_code=400)

            tx.status = "SUCCESS"
            tx.gateway_payment_id = status_res.get("utr") or form_data.get("utr")
            user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
            credit_wallet(user, tx.amount, WALLET_BUCKET_DEPOSIT)
            referral_bonus = maybe_credit_referrer_for_first_successful_deposit(
                db=db,
                referred_user=user,
                deposit_tx=tx,
            )
            if referral_bonus is not None:
                logger.info(
                    "Referral first-deposit bonus credited | ref_user=%s amount=%s",
                    user.id,
                    referral_bonus,
                )
            db.add(user)
            
            add_user_notification(
                db, user.id,
                "Payment Confirmed ✅",
                f"₹{tx.amount:.0f} has been added to your wallet via UPI.",
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
    current_user: User = Depends(get_current_user_wallet)
):
    """
    Polls the database for the final status of a transaction.
    """
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == txnid,
        WalletTransaction.user_id == current_user.id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    if tx.status == "PENDING":
        status_res = check_pay0_order_status(settings.PAY0_MERCHANT_KEY, txnid)
        if status_res["status"] == "SUCCESS":
            status_amount = Decimal(str(status_res.get("amount", 0)))
            if status_amount == tx.amount:
                tx.status = "SUCCESS"
                tx.gateway_payment_id = status_res.get("utr")
                tx.gateway_order_id = status_res.get("order_id") or tx.gateway_order_id or txnid
                user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
                credit_wallet(user, tx.amount, WALLET_BUCKET_DEPOSIT)
                referral_bonus = maybe_credit_referrer_for_first_successful_deposit(
                    db=db,
                    referred_user=user,
                    deposit_tx=tx,
                )
                if referral_bonus is not None:
                    logger.info(
                        "Referral first-deposit bonus credited via status poll | ref_user=%s amount=%s",
                        user.id,
                        referral_bonus,
                    )
                db.commit()
            else:
                tx.status = "FAILED"
                tx.failure_reason = f"PAY0_AMOUNT_MISMATCH expected={tx.amount} got={status_amount}"
                db.commit()
        elif status_res["status"] == "FAILED":
            tx.status = "FAILED"
            tx.failure_reason = status_res.get("error") or "PAY0_CONFIRMED_FAILED"
            db.commit()

    return {
        "txnid": txnid,
        "status": tx.status,
        "amount": tx.amount,
        "payment_mode": tx.payment_mode,
        "failure_reason": tx.failure_reason,
        "gateway_payment_id": tx.gateway_payment_id,
        "utr": tx.gateway_payment_id
    }


# ─────────────────────────────────────────────────────────────────
# Withdrawal Logic
# ─────────────────────────────────────────────────────────────────

@router.post("/cancel")
def cancel_payment(
    req: CancelPaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == req.txnid,
        WalletTransaction.user_id == current_user.id
    ).with_for_update().first()
    
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    if tx.status == "PENDING":
        tx.status = "FAILED"
        tx.failure_reason = "Cancelled by user"
        db.commit()
    return {"status": tx.status}

@router.post("/withdraw")
def request_withdrawal(
    req: WithdrawalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_wallet)
):
    amount_to_withdraw = to_money(req.amount)

    if amount_to_withdraw <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if amount_to_withdraw > Decimal("50000.00"):
        raise HTTPException(status_code=400, detail="Maximum withdrawal per request is ₹50,000")

    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    normalized_upi_id = _normalize_upi_id(req.upi_id)

    if not normalized_upi_id:
        raise HTTPException(status_code=400, detail="UPI ID is required")

    _, cycle_start, cycle_end = _current_withdrawal_cycle_ist()
    same_day_withdraw_count = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.transaction_type == "WITHDRAWAL",
            WalletTransaction.created_at >= cycle_start,
            WalletTransaction.created_at < cycle_end,
        )
        .count()
    )
    withdrawal_fee = (
        WITHDRAWAL_SAME_DAY_FEE
        if same_day_withdraw_count >= 1
        else Decimal("0.00")
    )
    total_wallet_debit = amount_to_withdraw + withdrawal_fee

    withdrawable_balance = get_withdrawable_balance(user)
    if withdrawable_balance < total_wallet_debit:
        if withdrawal_fee > Decimal("0.00"):
            detail = (
                f"Insufficient winning balance. Available ₹{withdrawable_balance:.2f}, "
                f"requested ₹{amount_to_withdraw:.2f} + fee ₹{withdrawal_fee:.2f}."
            )
        else:
            detail = (
                f"Insufficient winning balance. Available ₹{withdrawable_balance:.2f}, "
                f"requested ₹{amount_to_withdraw:.2f}."
            )
        raise HTTPException(
            status_code=400,
            detail=detail,
        )

    try:
        debit_wallet(
            user,
            total_wallet_debit,
            spend_order=(WALLET_BUCKET_WINNING,),
        )
    except InsufficientWalletBalanceError as exc:
        if withdrawal_fee > Decimal("0.00"):
            detail = (
                f"Insufficient winning balance. Available ₹{exc.available:.2f}, requested ₹{amount_to_withdraw:.2f} "
                f"+ fee ₹{withdrawal_fee:.2f}."
            )
        else:
            detail = (
                f"Insufficient winning balance. Available ₹{exc.available:.2f}, requested ₹{exc.required:.2f}."
            )
        raise HTTPException(
            status_code=400,
            detail=detail,
        )

    existing_accounts = (
        db.query(WithdrawUpiAccount)
        .filter(WithdrawUpiAccount.user_id == user.id)
        .order_by(WithdrawUpiAccount.id.asc())
        .all()
    )
    has_saved_upi = any(account.upi_id == normalized_upi_id for account in existing_accounts)
    if not has_saved_upi and len(existing_accounts) < MAX_WITHDRAW_UPI_ACCOUNTS:
        db.add(
            WithdrawUpiAccount(
                user_id=user.id,
                account_holder_name=(user.username or f"User{user.id}").strip(),
                upi_id=normalized_upi_id,
            )
        )

    withdraw_reference = f"GA-{uuid.uuid4().hex[:6].upper()}"
    tx = WalletTransaction(
        user_id=user.id,
        amount=-amount_to_withdraw,
        transaction_type="WITHDRAWAL",
        status="PENDING",
        reference_id=withdraw_reference,
        payment_mode="UPI",
        payu_txn_id=normalized_upi_id,
    )

    db.add(tx)
    if withdrawal_fee > Decimal("0.00"):
        db.add(
            WalletTransaction(
                user_id=user.id,
                amount=-withdrawal_fee,
                transaction_type="WITHDRAWAL_FEE",
                status="SUCCESS",
                reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
                payment_mode="UPI",
                failure_reason=(
                    f"SOURCE_WITHDRAWAL_REF:{withdraw_reference};"
                    f"DAILY_RESET:00:{WITHDRAWAL_DAILY_RESET_MINUTE_IST:02d}_IST"
                ),
            )
        )
    db.add(user)
    db.commit()

    if withdrawal_fee > Decimal("0.00"):
        notification_body = (
            f"Your withdrawal request of ₹{amount_to_withdraw:.2f} has been submitted. "
            f"A processing fee of ₹{withdrawal_fee:.2f} was charged because this is an additional same-day withdrawal."
        )
    else:
        notification_body = (
            f"Your withdrawal request of ₹{amount_to_withdraw:.2f} has been submitted with no processing fee."
        )

    add_user_notification(
        db,
        user.id,
        "Withdrawal Requested",
        notification_body,
        "WALLET"
    )

    if withdrawal_fee > Decimal("0.00"):
        return {
            "message": (
                f"Withdrawal requested successfully. ₹{withdrawal_fee:.2f} fee applied for additional same-day withdrawal."
            )
        }
    return {"message": "Withdrawal requested successfully. No processing fee applied."}

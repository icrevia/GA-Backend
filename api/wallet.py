from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import List
import uuid

from api.deps import get_db, get_current_user, get_current_active_admin
from models.user import User
from models.wallet import WalletTransaction
from schemas.wallet import AddMoneyRequest, PayUInitResponse, WithdrawalRequest, WalletTransactionResponse, WalletBalanceResponse
from services.payu import generate_payu_hash, verify_payu_hash
from core.config import settings

router = APIRouter()

@router.get("/balance", response_model=WalletBalanceResponse)
def get_balance(current_user: User = Depends(get_current_user)):
    return {"balance": current_user.wallet_balance}

@router.get("/transactions", response_model=List[WalletTransactionResponse])
def get_transactions(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(WalletTransaction).filter(WalletTransaction.user_id == current_user.id).order_by(WalletTransaction.created_at.desc()).all()

@router.post("/add-money/init", response_model=PayUInitResponse)
def init_add_money(
    req: AddMoneyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if req.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum recharge amount is \u20b91")
        
    txnid = f"ZEX_{uuid.uuid4().hex[:12].upper()}"
    productinfo = "ZexPlay Wallet Recharge"
    
    # Create pending transaction
    tx = WalletTransaction(
        user_id=current_user.id,
        amount=req.amount,
        transaction_type="ADD_MONEY",
        status="PENDING",
        reference_id=txnid
    )
    db.add(tx)
    db.commit()
    
    payu_hash = generate_payu_hash(
        txnid=txnid,
        amount=req.amount,
        productinfo=productinfo,
        firstname=current_user.username,
        email=current_user.email
    )
    
    # Example urls, usually App endpoints or deep links
    surl = "https://web-production-051ba.up.railway.app/api/v1/wallet/payu/success" 
    furl = "https://web-production-051ba.up.railway.app/api/v1/wallet/payu/failure"
    
    return {
        "txnid": txnid,
        "amount": req.amount,
        "productinfo": productinfo,
        "firstname": current_user.username,
        "email": current_user.email,
        "phone": "9999999999",
        "surl": surl,
        "furl": furl,
        "hash": payu_hash,
        "key": settings.PAYU_MERCHANT_KEY,
        "action": f"{settings.PAYU_BASE_URL}/_payment"
    }

@router.get("/payu/redirect/{txnid}", response_class=HTMLResponse)
def payu_redirect(txnid: str, vpa: str | None = None, db: Session = Depends(get_db)):
    tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")
        
    user = db.query(User).filter(User.id == tx.user_id).first()
    
    productinfo = "ZexPlay Wallet Recharge"
    payu_hash = generate_payu_hash(
        txnid=tx.reference_id,
        amount=tx.amount,
        productinfo=productinfo,
        firstname=user.username,
        email=user.email
    )
    
    surl = "https://web-production-051ba.up.railway.app/api/v1/wallet/payu/success" 
    furl = "https://web-production-051ba.up.railway.app/api/v1/wallet/payu/failure"
    
    seamless_fields = ""
    if vpa:
        seamless_fields = f"""
            <input type="hidden" name="pg" value="UPI" />
            <input type="hidden" name="bankcode" value="UPI" />
            <input type="hidden" name="vpa" value="{vpa}" />
        """
    
    html_content = f"""
    <html>
      <head><title>Secure Transfer</title></head>
      <body onload="document.forms['payuForm'].submit();" style="background:#0D0E12; color:#FFB800; font-family:sans-serif; text-align:center; padding-top:100px;">
        <h2>Connecting to Secure Arena Gateway...</h2>
        <form action="{settings.PAYU_BASE_URL}/_payment" method="post" name="payuForm">
            <input type="hidden" name="key" value="{settings.PAYU_MERCHANT_KEY}" />
            <input type="hidden" name="txnid" value="{tx.reference_id}" />
            <input type="hidden" name="amount" value="{tx.amount:.2f}" />
            <input type="hidden" name="productinfo" value="{productinfo}" />
            <input type="hidden" name="firstname" value="{user.username}" />
            <input type="hidden" name="email" value="{user.email}" />
            <input type="hidden" name="phone" value="9999999999" />
            <input type="hidden" name="surl" value="{surl}" />
            <input type="hidden" name="furl" value="{furl}" />
            <input type="hidden" name="hash" value="{payu_hash}" />
            {seamless_fields}
        </form>
      </body>
    </html>
    """
    return html_content

@router.post("/payu/webhook")
async def payu_webhook(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    
    txnid = form.get("txnid")
    amount = float(form.get("amount", 0))
    productinfo = form.get("productinfo")
    firstname = form.get("firstname")
    email = form.get("email")
    status = form.get("status")
    received_hash = form.get("hash")
    
    is_valid = verify_payu_hash(txnid, amount, productinfo, firstname, email, status, received_hash)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid hash")
        
    # Lock transaction row
    tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    if tx.status != "PENDING":
        return {"message": "Transaction already processed"}
        
    if status == "success":
        tx.status = "SUCCESS"
        # Update user wallet balance atomically
        user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
        user.wallet_balance += tx.amount
        db.add(user)
        tx.status = "FAILED"
        
    db.add(tx)
    db.commit()
    return {"message": "Webhook processed"}

@router.post("/payu/success", response_class=HTMLResponse)
@router.post("/payu/failure", response_class=HTMLResponse)
async def payu_return_handler(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    txnid = form.get("txnid")
    status = form.get("status")
    
    if txnid:
        # We just forcefully mark it FAILED if it comes through failure url or status is not success
        # For success, we let the actual webhook do the database insertion strictly to avoid double crediting
        if status != "success" or request.url.path.endswith("failure"):
            tx = db.query(WalletTransaction).filter(WalletTransaction.reference_id == txnid).first()
            if tx and tx.status == "PENDING":
                tx.status = "FAILED"
                db.add(tx)
                db.commit()
                
    # Return simple HTML to let the Android WebView gracefully detect the endpoint
    bg_color = "#16A34A" if status == "success" else "#EF4444"
    return HTMLResponse(f"""
    <html><body style="background:#0D0E12; color:white; text-align:center; padding-top:50px;">
        <h2 style="color:{bg_color};">{'Payment Successful!' if status == 'success' else 'Payment Failed!'}</h2>
        <p>You can now close this screen.</p>
    </body></html>
    """)

@router.post("/withdraw")
def request_withdrawal(
    req: WithdrawalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
        
    # Lock user
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    
    if user.wallet_balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")
        
    # Deduct balance immediately to prevent double spend
    user.wallet_balance -= req.amount
    
    # Save UPI ID
    user.upi_id = req.upi_id
    
    tx = WalletTransaction(
        user_id=user.id,
        amount=-req.amount,
        transaction_type="WITHDRAWAL",
        status="PENDING", # Requires admin approval
        reference_id=f"WITHDRAW_{uuid.uuid4().hex[:8].upper()}"
    )
    
    db.add(tx)
    db.add(user)
    db.commit()
    
    return {"message": "Withdrawal requested successfully. Waiting for admin approval."}

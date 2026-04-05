import ast
import astunparse

source_file = "tmp_auth_backup.py"
dest_file = "api/auth.py"

with open(source_file, "r", encoding="utf-8") as f:
    orig_code = f.read()

# Instead of AST, let's use a simpler text replacement that preserves exact formatting for untouched parts
import re

new_code = orig_code

# 1. Add _pending_signups
new_code = new_code.replace(
    "_otp_store: dict[str, str] = {}",
    "_otp_store: dict[str, str] = {}\n_pending_signups: dict[str, dict] = {}"
)

# 2. Extract out signup and verify_otp functions and replace them
# Using regex with MULTILINE to replace functions

# We know where signup starts: @router.post("/signup", response_model=SignupResponse)
# And it ends before @router.post("/send-otp")
signup_regex = re.compile(
    r'@router\.post\("/signup", response_model=SignupResponse\).*?(?=@router\.post\("/send-otp"\))',
    re.DOTALL
)

new_signup_logic = '''@router.post("/signup")
@limiter.limit("5/minute")  # Rate limit: 5 signups per minute per IP
def signup(request: Request, user_in: UserCreate, db: Session = Depends(get_db)) -> Any:
    email = user_in.email.strip().lower().split('\\n')[0]
    phone = _normalize_signup_phone(user_in.phone_number)

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    if db.query(User).filter(User.phone_number == phone).first():
        raise HTTPException(status_code=400, detail="Phone number already in use")

    if db.query(User).filter(User.username == user_in.username).first():
        raise HTTPException(status_code=400, detail="Username taken")

    # Store pending signup instead of saving to DB immediately
    _pending_signups[phone] = {
        "username": user_in.username,
        "email": email,
        "phone_number": phone,
        "referral_code": user_in.referral_code,
    }

    # Automatically trigger OTP sending
    try:
        result = otp_service.send_otp(phone)
        verification_id = result["data"]["verificationId"]
        _otp_store[phone] = verification_id
        logger.info(f"OTP automatically sent for pending signup {phone}, verificationId={verification_id}")
    except Exception as e:
        # If OTP service fails, remove pending signup
        _pending_signups.pop(phone, None)
        logger.error(f"Failed to send OTP during signup: {e}")
        raise HTTPException(status_code=503, detail="Failed to send OTP verification. Please try again.")

    return {
        "message": "OTP sent to phone for verification",
        "phone": phone,
        "status": "pending_verification"
    }

'''

new_code = signup_regex.sub(new_signup_logic, new_code)


verify_otp_regex = re.compile(
    r'@router\.post\("/verify-otp", response_model=SignupResponse\).*?(?=@router\.post\("/login", response_model=Any\))',
    re.DOTALL
)

new_verify_otp_logic = '''@router.post("/verify-otp", response_model=SignupResponse)
def verify_otp(
    request: Request,
    phone: str = Query(...),
    otp: str = Query(...),
    db: Session = Depends(get_db)
) -> Any:
    """Verify OTP via Message Central and return access token."""
    normalized_phone = _normalize_signup_phone(phone)

    verification_id = _otp_store.get(normalized_phone)
    if not verification_id:
        raise HTTPException(status_code=400, detail="OTP expired or not requested. Please resend.")

    valid = otp_service.verify_otp(verification_id, otp)
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # OTP verified — clean up
    _otp_store.pop(normalized_phone, None)
    
    user = None

    # Check if this is a pending signup
    if normalized_phone in _pending_signups:
        # Commit the user to the database now
        pending_data = _pending_signups.pop(normalized_phone)
        
        # 1. Generate unique referral code for the new user
        import string, random
        def generate_code():
            return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        ref_code = generate_code()
        while db.query(User).filter(User.referral_code == ref_code).first():
            ref_code = generate_code()

        # 2. Check for referrer
        referrer = None
        if pending_data["referral_code"]:
            referrer = db.query(User).filter(User.referral_code == pending_data["referral_code"].strip().upper()).first()
            if not referrer:
                 logger.warning(f"Invalid referral code used: {pending_data['referral_code']}")

        referrer_signup_bonus = Decimal("2.00")

        db_user = User(
            username=pending_data["username"],
            email=pending_data["email"],
            phone_number=pending_data["phone_number"],
            role="USER",
            referral_code=ref_code,
            referred_by_id=referrer.id if referrer else None,
            wallet_balance=Decimal("0.00")
        )

        db.add(db_user)
        db.commit()
        db.refresh(db_user)

        # 3. Handle Referrer instant signup payout (INR 2)
        if referrer:
            current_balance = Decimal(str(referrer.wallet_balance or 0))
            referrer.wallet_balance = current_balance + referrer_signup_bonus
            
            from models.wallet import WalletTransaction
            # Record Referrer Transaction
            db.add(WalletTransaction(
                user_id=referrer.id,
                amount=referrer_signup_bonus,
                transaction_type="REFERRAL_REWARD",
                status="SUCCESS",
                reference_id=f"REF_SIGNUP_{db_user.id}"
            ))
            
            from services.notifications import add_user_notification
            add_user_notification(
                db,
                referrer.id,
                "Referral Reward! 💎",
                (
                    f"Your friend {db_user.username} joined using your code. "
                    f"₹2 instant bonus added. Mission progress grows after their ₹50+ recharge."
                ),
                "WALLET"
            )
            db.commit()

        # Auto-assign one of 5 default avatars
        avatar_id = (db_user.id % 5) + 1
        db_user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{avatar_id}.png"
        db.commit()
        db.refresh(db_user)

        from services.notifications import add_user_notification
        add_user_notification(
            db,
            db_user.id,
            "Welcome to GamerzAdda",
            "Start your esports journey with India's fastest tournament platform. 🦾",
            "APP"
        )

        logger.info(f"New signup verified & created: user_id={db_user.id} username={db_user.username}")
        user = db_user
    else:
        # Existing login
        user = db.query(User).filter(User.phone_number == normalized_phone).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

    token_version = getattr(user, "token_version", 0) or 0
    return {
        "access_token": create_access_token({"sub": str(user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }

'''

new_code = verify_otp_regex.sub(new_verify_otp_logic, new_code)

if new_code == orig_code:
    print("WARNING: Replacement failed!")
else:
    with open(dest_file, "w", encoding="utf-8") as f:
        f.write(new_code)
    print("SUCCESS: auth.py rewritten.")

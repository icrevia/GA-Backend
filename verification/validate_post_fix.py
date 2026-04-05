import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Configure environment BEFORE importing app/settings modules.
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
os.chdir(BACKEND_ROOT)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(BACKEND_ROOT / 'post_fix_verify.db').as_posix()}")
os.environ.setdefault("APP_URL", "http://testserver")
os.environ.setdefault("ALLOWED_ORIGINS", "http://testserver")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret")
os.environ.setdefault("PAYU_MERCHANT_KEY", "payu_key")
os.environ.setdefault("PAYU_MERCHANT_SALT", "payu_salt")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.wallet as wallet_api
import core.database as db_core
from core.security import create_access_token, hash_password
from main import app
from models import user as user_model
from models import wallet as wallet_model
from models import tournament as tournament_model
from models import participant as participant_model
from models import config as config_model


# Rebuild DB engine with sqlite thread-safety for TestClient.
DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Monkeypatch shared DB handles used by dependencies.
db_core.engine = engine
db_core.SessionLocal = SessionLocal

# Ensure metadata is attached and tables exist.
db_core.Base.metadata.drop_all(bind=engine)
db_core.Base.metadata.create_all(bind=engine)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def reset_db() -> None:
    db_core.Base.metadata.drop_all(bind=engine)
    db_core.Base.metadata.create_all(bind=engine)


def make_user(db, username: str, email: str, role: str = "USER", wallet_balance: float = 0.0, password: str = "pass123"):
    u = user_model.User(
        username=username,
        email=email,
        phone_number=f"+9112345{abs(hash(username)) % 100000:05d}",
        hashed_password=hash_password(password),
        role=role,
        wallet_balance=wallet_balance,
        is_active=True,
        token_version=0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_token(u: user_model.User) -> str:
    tv = int(getattr(u, "token_version", 0) or 0)
    return create_access_token({"sub": str(u.id), "tv": tv})


def json_or_text(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def case_zp_pay_001(client: TestClient) -> dict[str, Any]:
    reset_db()

    order_registry: dict[str, dict[str, Any]] = {}
    payment_registry: dict[str, dict[str, Any]] = {}

    original_get_order = wallet_api.get_razorpay_order
    original_get_payment = wallet_api.get_razorpay_payment

    wallet_api.get_razorpay_order = lambda order_id: order_registry.get(order_id)
    wallet_api.get_razorpay_payment = lambda payment_id: payment_registry.get(payment_id)

    try:
        with SessionLocal() as db:
            user = make_user(db, "pay_user", "pay_user@example.com", wallet_balance=0.0)
            token = make_token(user)

            tx = wallet_model.WalletTransaction(
                user_id=user.id,
                amount=10000.00,
                transaction_type="ADD_MONEY",
                status="PENDING",
                reference_id="TXN_HIGH_001",
                gateway_order_id="order_secure_001",
                payment_mode="RAZORPAY",
            )
            db.add(tx)
            db.commit()

        secret = os.environ["RAZORPAY_KEY_SECRET"]

        # Variation A: tampered amount should be blocked.
        order_registry["order_secure_001"] = {
            "id": "order_secure_001",
            "receipt": "TXN_HIGH_001",
            "amount": 10000,  # 100 INR only, should mismatch tx amount (10000 INR)
            "currency": "INR",
        }
        payment_registry["pay_tampered_001"] = {
            "id": "pay_tampered_001",
            "order_id": "order_secure_001",
            "amount": 10000,
            "currency": "INR",
            "status": "captured",
        }
        sig_tampered = hmac.new(
            secret.encode(),
            "order_secure_001|pay_tampered_001".encode(),
            hashlib.sha256,
        ).hexdigest()

        tamper_resp = client.post(
            "/api/v1/wallet/razorpay/verify",
            json={
                "razorpay_order_id": "order_secure_001",
                "razorpay_payment_id": "pay_tampered_001",
                "razorpay_signature": sig_tampered,
            },
            headers=auth_header(token),
        )

        # Variation B: valid payment should credit once.
        order_registry["order_secure_001"]["amount"] = 1_000_000  # 10,000 INR
        payment_registry["pay_valid_001"] = {
            "id": "pay_valid_001",
            "order_id": "order_secure_001",
            "amount": 1_000_000,
            "currency": "INR",
            "status": "captured",
        }
        sig_valid = hmac.new(
            secret.encode(),
            "order_secure_001|pay_valid_001".encode(),
            hashlib.sha256,
        ).hexdigest()

        valid_resp = client.post(
            "/api/v1/wallet/razorpay/verify",
            json={
                "razorpay_order_id": "order_secure_001",
                "razorpay_payment_id": "pay_valid_001",
                "razorpay_signature": sig_valid,
            },
            headers=auth_header(token),
        )

        # Variation C: replay same payment id must not credit twice.
        replay_resp = client.post(
            "/api/v1/wallet/razorpay/verify",
            json={
                "razorpay_order_id": "order_secure_001",
                "razorpay_payment_id": "pay_valid_001",
                "razorpay_signature": sig_valid,
            },
            headers=auth_header(token),
        )

        with SessionLocal() as db:
            user_after = db.query(user_model.User).filter(user_model.User.email == "pay_user@example.com").first()
            tx_after = db.query(wallet_model.WalletTransaction).filter(wallet_model.WalletTransaction.reference_id == "TXN_HIGH_001").first()

        return {
            "bug_id": "ZP-PAY-001",
            "request": {
                "tampered_then_valid_then_replay": True,
                "path": "/api/v1/wallet/razorpay/verify",
            },
            "response": {
                "tampered": {"status_code": tamper_resp.status_code, "body": json_or_text(tamper_resp)},
                "valid": {"status_code": valid_resp.status_code, "body": json_or_text(valid_resp)},
                "replay": {"status_code": replay_resp.status_code, "body": json_or_text(replay_resp)},
            },
            "proof": {
                "tamper_blocked": tamper_resp.status_code == 400,
                "legit_credit_once": valid_resp.status_code == 200 and float(user_after.wallet_balance) == 10000.0,
                "replay_no_double_credit": replay_resp.status_code == 200 and float(user_after.wallet_balance) == 10000.0,
                "tx_status_after": tx_after.status,
                "gateway_payment_id": tx_after.gateway_payment_id,
            },
        }
    finally:
        wallet_api.get_razorpay_order = original_get_order
        wallet_api.get_razorpay_payment = original_get_payment


def case_zp_pay_002(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        admin = make_user(db, "admin_bulk", "admin_bulk@example.com", role="ADMIN", wallet_balance=0.0)
        user = make_user(db, "wd_user_bulk", "wd_user_bulk@example.com", wallet_balance=600.0)
        admin_token = make_token(admin)

        wd = wallet_model.WalletTransaction(
            user_id=user.id,
            amount=-400.00,
            transaction_type="WITHDRAWAL",
            status="PENDING",
            reference_id="WD_BULK_001",
        )
        db.add(wd)
        db.commit()
        db.refresh(wd)

    resp = client.post("/api/v1/admin/transactions/reject-all-pending", headers=auth_header(admin_token))

    with SessionLocal() as db:
        user_after = db.query(user_model.User).filter(user_model.User.email == "wd_user_bulk@example.com").first()
        tx_after = db.query(wallet_model.WalletTransaction).filter(wallet_model.WalletTransaction.reference_id == "WD_BULK_001").first()
        refund_tx = db.query(wallet_model.WalletTransaction).filter(
            wallet_model.WalletTransaction.reference_id == f"REFUND_WD_{tx_after.id}"
        ).first()

    return {
        "bug_id": "ZP-PAY-002",
        "request": {
            "method": "POST",
            "path": "/api/v1/admin/transactions/reject-all-pending",
        },
        "response": {
            "status_code": resp.status_code,
            "body": json_or_text(resp),
        },
        "proof": {
            "wallet_balance_after": float(user_after.wallet_balance),
            "tx_status_after": tx_after.status,
            "refund_tx_found": refund_tx is not None,
            "refund_applied": float(user_after.wallet_balance) == 1000.0,
        },
    }


def case_zp_pay_003(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        admin = make_user(db, "admin_mark", "admin_mark@example.com", role="ADMIN")
        user = make_user(db, "wd_user_mark", "wd_user_mark@example.com", wallet_balance=500.0)
        admin_token = make_token(admin)

        wd = wallet_model.WalletTransaction(
            user_id=user.id,
            amount=-200.00,
            transaction_type="WITHDRAWAL",
            status="PENDING",
            reference_id="WD_MARK_001",
        )
        db.add(wd)
        db.commit()
        db.refresh(wd)

    first_resp = client.post(f"/api/v1/admin/transactions/{wd.id}/mark-failed", headers=auth_header(admin_token))
    replay_resp = client.post(f"/api/v1/admin/transactions/{wd.id}/mark-failed", headers=auth_header(admin_token))

    with SessionLocal() as db:
        user_after = db.query(user_model.User).filter(user_model.User.email == "wd_user_mark@example.com").first()
        tx_after = db.query(wallet_model.WalletTransaction).filter(wallet_model.WalletTransaction.reference_id == "WD_MARK_001").first()
        refund_tx = db.query(wallet_model.WalletTransaction).filter(
            wallet_model.WalletTransaction.reference_id == f"REFUND_WD_{tx_after.id}"
        ).first()

    return {
        "bug_id": "ZP-PAY-003",
        "request": {
            "method": "POST",
            "path": f"/api/v1/admin/transactions/{wd.id}/mark-failed",
            "replay": True,
        },
        "response": {
            "first": {"status_code": first_resp.status_code, "body": json_or_text(first_resp)},
            "replay": {"status_code": replay_resp.status_code, "body": json_or_text(replay_resp)},
        },
        "proof": {
            "wallet_balance_after": float(user_after.wallet_balance),
            "tx_status_after": tx_after.status,
            "refund_tx_found": refund_tx is not None,
            "refund_applied_once": float(user_after.wallet_balance) == 700.0,
            "replay_blocked": replay_resp.status_code == 400,
        },
    }


def case_zp_aud_004(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        admin = make_user(db, "admin_clear", "admin_clear@example.com", role="ADMIN")
        user = make_user(db, "clear_user", "clear_user@example.com", wallet_balance=100.0)
        admin_token = make_token(admin)

        tx1 = wallet_model.WalletTransaction(
            user_id=user.id,
            amount=50.00,
            transaction_type="ADD_MONEY",
            status="SUCCESS",
            reference_id="CLR_001",
        )
        tx2 = wallet_model.WalletTransaction(
            user_id=user.id,
            amount=-10.00,
            transaction_type="JOIN_TOURNAMENT",
            status="SUCCESS",
            reference_id="CLR_002",
        )
        db.add(tx1)
        db.add(tx2)
        db.commit()

        before_count = db.query(wallet_model.WalletTransaction).count()

    resp = client.post("/api/v1/admin/transactions/clear-history", headers=auth_header(admin_token))

    with SessionLocal() as db:
        after_count = db.query(wallet_model.WalletTransaction).count()

    return {
        "bug_id": "ZP-AUD-004",
        "request": {
            "method": "POST",
            "path": "/api/v1/admin/transactions/clear-history",
        },
        "response": {
            "status_code": resp.status_code,
            "body": json_or_text(resp),
        },
        "proof": {
            "tx_count_before": before_count,
            "tx_count_after": after_count,
            "deletion_blocked": resp.status_code == 403 and after_count == before_count,
        },
    }


def case_zp_sec_005(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        user = make_user(db, "qtoken_user", "qtoken_user@example.com", wallet_balance=321.0)
        token = make_token(user)

    resp = client.get(f"/api/v1/wallet/balance?token={token}")

    return {
        "bug_id": "ZP-SEC-005",
        "request": {
            "method": "GET",
            "path": "/api/v1/wallet/balance?token=<JWT>",
        },
        "response": {
            "status_code": resp.status_code,
            "body": json_or_text(resp),
        },
        "proof": {
            "query_token_rejected": resp.status_code == 401,
        },
    }


def case_zp_sec_006(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        user = make_user(db, "ws_user", "ws_user@example.com", wallet_balance=0.0)
        token = make_token(user)

    query_token_ok = False
    query_token_frame: dict[str, Any] | None = None
    try:
        with client.websocket_connect(f"/api/v1/ws/ws?token={token}") as ws:
            query_token_frame = ws.receive_json()
            query_token_ok = query_token_frame.get("type") == "connected"
    except Exception as e:
        query_token_frame = {"error": str(e)}

    protocol_ok = False
    protocol_frame: dict[str, Any] | None = None
    try:
        with client.websocket_connect(
            "/api/v1/ws/ws",
            subprotocols=["GamerzAdda.v1", f"token.{token}"],
        ) as ws:
            protocol_frame = ws.receive_json()
            protocol_ok = protocol_frame.get("type") == "connected"
    except Exception as e:
        protocol_frame = {"error": str(e)}

    return {
        "bug_id": "ZP-SEC-006",
        "request": {
            "query_path": "/api/v1/ws/ws?token=<JWT>",
            "protocol_auth": "Sec-WebSocket-Protocol: GamerzAdda.v1, token.<JWT>",
        },
        "response": {
            "query_token_frame": query_token_frame,
            "protocol_frame": protocol_frame,
        },
        "proof": {
            "query_token_rejected": not query_token_ok,
            "protocol_token_accepted": protocol_ok,
        },
    }


def case_zp_sec_009(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        make_user(db, "enum_user", "enum_user@example.com", wallet_balance=0.0, password="CorrectPass1")

    unknown_payload = {"email": "unknown@example.com", "password": "WrongPass1"}
    known_wrong_payload = {"email": "enum_user@example.com", "password": "WrongPass1"}

    resp_unknown = client.post("/api/v1/auth/login", json=unknown_payload)
    resp_known_wrong = client.post("/api/v1/auth/login", json=known_wrong_payload)

    detail_unknown = (json_or_text(resp_unknown) or {}).get("detail") if isinstance(json_or_text(resp_unknown), dict) else None
    detail_known = (json_or_text(resp_known_wrong) or {}).get("detail") if isinstance(json_or_text(resp_known_wrong), dict) else None

    return {
        "bug_id": "ZP-SEC-009",
        "request": {
            "unknown_user_payload": unknown_payload,
            "known_user_wrong_password_payload": known_wrong_payload,
        },
        "response": {
            "unknown_user": {"status_code": resp_unknown.status_code, "body": json_or_text(resp_unknown)},
            "known_user_wrong_password": {"status_code": resp_known_wrong.status_code, "body": json_or_text(resp_known_wrong)},
        },
        "proof": {
            "enumeration_blocked": (
                resp_unknown.status_code == resp_known_wrong.status_code == 401
                and detail_unknown == detail_known == "Invalid credentials"
            ),
        },
    }


def case_zp_pay_010(client: TestClient) -> dict[str, Any]:
    reset_db()
    with SessionLocal() as db:
        admin = make_user(db, "admin_manual", "admin_manual@example.com", role="ADMIN")
        user = make_user(db, "manual_user", "manual_user@example.com", wallet_balance=0.0)
        admin_token = make_token(admin)

        tx = wallet_model.WalletTransaction(
            user_id=user.id,
            amount=750.0,
            transaction_type="ADD_MONEY",
            status="PENDING",
            reference_id="MANUAL_001",
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)

    resp = client.post(f"/api/v1/admin/transactions/{tx.id}/manual-credit", headers=auth_header(admin_token))

    with SessionLocal() as db:
        user_after = db.query(user_model.User).filter(user_model.User.email == "manual_user@example.com").first()
        tx_after = db.query(wallet_model.WalletTransaction).filter(wallet_model.WalletTransaction.reference_id == "MANUAL_001").first()

    return {
        "bug_id": "ZP-PAY-010",
        "request": {
            "method": "POST",
            "path": f"/api/v1/admin/transactions/{tx.id}/manual-credit",
        },
        "response": {
            "status_code": resp.status_code,
            "body": json_or_text(resp),
        },
        "proof": {
            "manual_credit_blocked": resp.status_code == 403,
            "wallet_balance_after": float(user_after.wallet_balance),
            "tx_status_after": tx_after.status,
        },
    }


def case_static_checks() -> list[dict[str, Any]]:
    results = []

    android_manifest = (REPO_ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    results.append(
        {
            "bug_id": "ZP-SEC-007",
            "proof": {
                "cleartext_disabled": "android:usesCleartextTraffic=\"false\"" in android_manifest,
            },
        }
    )

    app_nav = (REPO_ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "GamerzAdda" / "ui" / "navigation" / "AppNavigation.kt").read_text(encoding="utf-8")
    ccav_screen = (REPO_ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "GamerzAdda" / "ui" / "screens" / "wallet" / "CCAvenueWebViewScreen.kt").read_text(encoding="utf-8")
    finance_page = (REPO_ROOT / "admin-web" / "app" / "finance" / "page.tsx").read_text(encoding="utf-8")
    call_context = (REPO_ROOT / "admin-web" / "context" / "CallContext.tsx").read_text(encoding="utf-8")
    results.append(
        {
            "bug_id": "ZP-FUNC-008",
            "proof": {
                "hardcoded_android_base_url_removed": "web-production-051ba.up.railway.app" not in app_nav,
                "hardcoded_ccavenue_redirect_removed": "web-production-051ba.up.railway.app/api/v1/wallet/ccavenue/return" not in ccav_screen,
                "hardcoded_admin_ws_fallback_removed": "web-production-051ba.up.railway.app" not in finance_page and "web-production-051ba.up.railway.app" not in call_context,
            },
        }
    )

    root_main_exists = (REPO_ROOT / "main.py").exists()
    root_procfile_exists = (REPO_ROOT / "Procfile").exists()
    root_config = (REPO_ROOT / "core" / "config.py").read_text(encoding="utf-8")

    results.append(
        {
            "bug_id": "ZP-SEC-020",
            "proof": {
                "wildcard_cors_with_credentials_removed": not root_main_exists,
                "hardcoded_secret_in_source_removed": "GamerzAdda_Super_Secure_JWT_Key_2026_@" not in root_config,
            },
        }
    )

    results.append(
        {
            "bug_id": "ZP-ARCH-019",
            "proof": {
                "single_procfile_entrypoint": not root_procfile_exists and (REPO_ROOT / "backend" / "Procfile").exists(),
                "single_main_entrypoint": not root_main_exists and (REPO_ROOT / "backend" / "main.py").exists(),
            },
        }
    )

    return results


def main() -> None:
    traces: list[dict[str, Any]] = []

    with TestClient(app) as client:
        traces.append(case_zp_pay_001(client))
        traces.append(case_zp_pay_002(client))
        traces.append(case_zp_pay_003(client))
        traces.append(case_zp_aud_004(client))
        traces.append(case_zp_sec_005(client))
        traces.append(case_zp_sec_006(client))
        traces.append(case_zp_sec_009(client))
        traces.append(case_zp_pay_010(client))

    traces.extend(case_static_checks())

    out_dir = BACKEND_ROOT / "verification"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "post_fix_validation_traces.json"

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "db_url": DB_URL,
        "results": traces,
    }

    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"WROTE {out_file}")

    for item in traces:
        bug_id = item.get("bug_id")
        proof = item.get("proof", {})
        print(f"[{bug_id}] {proof}")


if __name__ == "__main__":
    main()

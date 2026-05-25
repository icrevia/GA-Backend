import hmac
import logging
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from api.admin import process_withdrawal_approval, process_withdrawal_rejection
from api.deps import get_db
from core.config import settings
from core.websockets import manager as ws_manager
from models.wallet import WalletTransaction
from services.ledger_bot import (
    answer_callback_query,
    build_withdrawal_resolution_text,
    edit_message_text,
    send_message,
    is_ledger_admin_telegram_id,
    parse_withdrawal_callback_data,
)
import re

logger = logging.getLogger("GamerzAdda.ledger.webhook")
router = APIRouter()


def _has_valid_telegram_secret(request: Request) -> bool:
    expected = str(settings.LEDGER_WEBHOOK_SECRET or "").strip()
    if not expected:
        return True

    received = (request.headers.get("x-telegram-bot-api-secret-token") or "").strip()
    return bool(received and hmac.compare_digest(received, expected))


@router.post("/webhook")
def ledger_bot_webhook(
    request: Request,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if not _has_valid_telegram_secret(request):
        logger.warning("Rejected ledger webhook request due to invalid secret token")
        return {"ok": True}

    if not isinstance(payload, dict):
        return {"ok": True}

    message_event = payload.get("message")
    if isinstance(message_event, dict) and "reply_to_message" in message_event:
        reply_to_message = message_event["reply_to_message"]
        original_text = str(reply_to_message.get("text", ""))
        match = re.search(r"REJECT WITHDRAWAL \[TxID: (\d+)\]", original_text)
        
        if match:
            transaction_id = int(match.group(1))
            reason_text = str(message_event.get("text", "")).strip()
            
            actor = message_event.get("from") if isinstance(message_event.get("from"), dict) else {}
            actor_chat_id = str(actor.get("id") or "").strip()
            actor_username = str(actor.get("username") or "").strip()
            actor_label = f"@{actor_username}" if actor_username else f"tg:{actor_chat_id}"

            if not actor_chat_id or not is_ledger_admin_telegram_id(actor_chat_id):
                return {"ok": True}

            tx = (
                db.query(WalletTransaction)
                .filter(WalletTransaction.id == transaction_id)
                .with_for_update()
                .first()
            )

            if tx and tx.transaction_type == "WITHDRAWAL" and tx.status == "PENDING":
                try:
                    refunded_amount = process_withdrawal_rejection(
                        db,
                        tx,
                        actor_label=actor_label,
                        reason_code=reason_text or "REJECTED_BY_TELEGRAM_ADMIN",
                        source="TELEGRAM",
                    )
                    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
                    
                    edit_message_text(
                        reply_to_message.get("chat", {}).get("id"),
                        reply_to_message.get("message_id"),
                        build_withdrawal_resolution_text(
                            transaction_id=tx.id,
                            user_id=tx.user_id,
                            amount=tx.amount,
                            upi_id=tx.payu_txn_id,
                            status=tx.status,
                            actor_label=actor_label,
                            refunded_amount=refunded_amount,
                        ) + f"\n\nDecline Reason: {reason_text}",
                    )
                except Exception as exc:
                    db.rollback()
                    logger.exception("Failed to process reply rejection: %s", exc)
                    
            return {"ok": True}

    callback = payload.get("callback_query")
    if not isinstance(callback, dict):
        return {"ok": True}

    callback_query_id = str(callback.get("id") or "").strip()
    actor = callback.get("from") if isinstance(callback.get("from"), dict) else {}
    actor_chat_id = str(actor.get("id") or "").strip()
    actor_username = str(actor.get("username") or "").strip()
    actor_label = f"@{actor_username}" if actor_username else f"tg:{actor_chat_id}"

    if not actor_chat_id or not is_ledger_admin_telegram_id(actor_chat_id):
        logger.warning("Unauthorized ledger callback attempt from telegram_id=%s", actor_chat_id or "-")
        answer_callback_query(
            callback_query_id,
            "You are not authorized for this action.",
            show_alert=True,
        )
        return {"ok": True}

    parsed_callback = parse_withdrawal_callback_data(str(callback.get("data") or ""))
    if not parsed_callback:
        answer_callback_query(
            callback_query_id,
            "Invalid or expired action payload.",
            show_alert=True,
        )
        return {"ok": True}

    action_code, transaction_id = parsed_callback

    tx = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.id == transaction_id)
        .with_for_update()
        .first()
    )

    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    message_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    message_chat_id = message_chat.get("id")
    message_id = message.get("message_id")

    if not tx or tx.transaction_type != "WITHDRAWAL":
        answer_callback_query(callback_query_id, "Withdrawal transaction not found.", show_alert=True)
        return {"ok": True}

    if tx.status != "PENDING":
        answer_callback_query(callback_query_id, f"Already processed: {tx.status}", show_alert=False)
        if message_chat_id is not None and message_id is not None:
            edit_message_text(
                message_chat_id,
                int(message_id),
                build_withdrawal_resolution_text(
                    transaction_id=tx.id,
                    user_id=tx.user_id,
                    amount=tx.amount,
                    upi_id=tx.payu_txn_id,
                    status=tx.status,
                    actor_label="Already processed",
                    refunded_amount=Decimal("0.00"),
                ),
            )
        return {"ok": True}

    try:
        if action_code == "A":
            process_withdrawal_approval(
                db,
                tx,
                actor_label=actor_label,
                source="TELEGRAM",
            )
            callback_text = "Withdrawal approved"
            refunded_amount = Decimal("0.00")
            
            background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
            answer_callback_query(callback_query_id, callback_text, show_alert=False)

            if message_chat_id is not None and message_id is not None:
                edit_message_text(
                    message_chat_id,
                    int(message_id),
                    build_withdrawal_resolution_text(
                        transaction_id=tx.id,
                        user_id=tx.user_id,
                        amount=tx.amount,
                        upi_id=tx.payu_txn_id,
                        status=tx.status,
                        actor_label=actor_label,
                        refunded_amount=refunded_amount,
                    ),
                )
        else:
            answer_callback_query(callback_query_id, "Please reply with reason", show_alert=False)
            if message_chat_id is not None:
                send_message(
                    message_chat_id,
                    f"REJECT WITHDRAWAL [TxID: {transaction_id}]\n\nPlease reply to this message with the decline reason to confirm rejection.",
                    reply_to_message_id=int(message_id) if message_id else None,
                    force_reply=True
                )
    except Exception as exc:
        db.rollback()
        logger.exception("Ledger callback action failed for tx=%s error=%s", transaction_id, exc)
        answer_callback_query(
            callback_query_id,
            "Failed to process action. Please retry from admin panel.",
            show_alert=True,
        )
        return {"ok": True}

    return {"ok": True}

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core.database import SyncSessionLocal
from models.participant import TournamentParticipant
from models.tournament import Tournament
from models.user import User
from models.wallet import WalletTransaction
from services.match_stats import leaderboard_prize_payment_mode
from services.wallet_balances import WALLET_BUCKET_WINNING, credit_wallet, ensure_wallet_buckets, to_money

SEED_TAG = "LB_SEED_20260422"
USER_COUNT = 12

CATEGORY_TO_GAME_NAME = {
    "free_fire": "Free Fire",
    "free_fire_max": "Free Fire Max",
    "clash_squad": "Clash Squad",
}

TIME_BUCKET_OFFSETS = {
    "today": timedelta(hours=2),
    "last_7_days": timedelta(days=3),
    "last_30_days": timedelta(days=15),
    "lifetime": timedelta(days=65),
}


def _ensure_dummy_users(db) -> list[User]:
    users: list[User] = []
    for idx in range(1, USER_COUNT + 1):
        username = f"lb_seed_{idx:02d}"
        email = f"{username}@gamerzadda.local"

        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                username=username,
                email=email,
                role="USER",
                is_active=True,
                bio=f"Season grinder #{idx}",
                wallet_balance=Decimal("0.00"),
                deposit_balance=Decimal("0.00"),
                winning_balance=Decimal("0.00"),
                bonus_balance=Decimal("0.00"),
            )
            db.add(user)
            db.flush()
        else:
            if not user.bio:
                user.bio = f"Season grinder #{idx}"
            if not user.is_active:
                user.is_active = True

        ensure_wallet_buckets(user)
        users.append(user)

    return users


def _ensure_seed_tournament(db, title: str, game_name: str, event_at: datetime) -> tuple[Tournament, bool]:
    tournament = db.query(Tournament).filter(Tournament.title == title).first()
    created = False
    if not tournament:
        tournament = Tournament(
            title=title,
            game_name=game_name,
            entry_fee=Decimal("10.00"),
            prize_pool=Decimal("500.00"),
            commission_percentage=Decimal("10.00"),
            per_kill_prize=Decimal("20.00"),
            match_time=event_at,
            status="COMPLETED",
            match_type="SOLO",
            max_slots=100,
            winner_id=None,
        )
        db.add(tournament)
        db.flush()
        created = True

    tournament.status = "COMPLETED"
    tournament.match_time = event_at
    tournament.updated_at = event_at
    db.add(tournament)
    return tournament, created


def _ensure_participant(db, tournament_id: int, user_id: int) -> None:
    participant = (
        db.query(TournamentParticipant)
        .filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == user_id,
        )
        .first()
    )
    if participant:
        return

    db.add(
        TournamentParticipant(
            tournament_id=tournament_id,
            user_id=user_id,
            game_username=f"seed_{user_id}",
            game_uid=f"seed_uid_{tournament_id}_{user_id}",
            account_level=50,
        )
    )


def _participant_user_ids_for_tournament(db, tournament_id: int) -> set[int]:
    rows = (
        db.query(TournamentParticipant.user_id)
        .filter(TournamentParticipant.tournament_id == tournament_id)
        .all()
    )
    return {int(row.user_id) for row in rows if row.user_id is not None}


def _ensure_prize_credit(
    db,
    *,
    user: User,
    tournament: Tournament,
    category: str,
    amount: Decimal,
    event_at: datetime,
) -> bool:
    reference_id = f"{SEED_TAG}-T{tournament.id}-U{user.id}"
    existing = db.query(WalletTransaction).filter(WalletTransaction.reference_id == reference_id).first()
    if existing:
        if existing.payment_mode != leaderboard_prize_payment_mode(category):
            existing.payment_mode = leaderboard_prize_payment_mode(category)
            db.add(existing)
        return False

    credit_wallet(user, to_money(amount), WALLET_BUCKET_WINNING)

    tx = WalletTransaction(
        user_id=user.id,
        amount=to_money(amount),
        transaction_type="PRIZE_WIN",
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode=leaderboard_prize_payment_mode(category),
        created_at=event_at,
    )
    db.add(user)
    db.add(tx)
    return True


def seed_leaderboard_dummy_data() -> None:
    now_utc = datetime.now(timezone.utc)

    created_users = 0
    created_tournaments = 0
    created_participants = 0
    created_credits = 0

    with SyncSessionLocal() as db:
        users_before = db.query(User.id).filter(User.username.like("lb_seed_%")).count()
        users = _ensure_dummy_users(db)
        users_after = db.query(User.id).filter(User.username.like("lb_seed_%")).count()
        created_users = max(users_after - users_before, 0)

        for cat_index, (category, game_name) in enumerate(CATEGORY_TO_GAME_NAME.items()):
            for bucket_index, (bucket, offset) in enumerate(TIME_BUCKET_OFFSETS.items()):
                event_at = now_utc - offset
                title = f"{SEED_TAG} | {game_name} | {bucket}"

                tournament, created = _ensure_seed_tournament(db, title=title, game_name=game_name, event_at=event_at)
                if created:
                    created_tournaments += 1

                start_index = (cat_index * 3 + bucket_index * 2) % len(users)
                participants = [users[(start_index + step) % len(users)] for step in range(8)]
                existing_participant_user_ids = _participant_user_ids_for_tournament(db, tournament.id)

                for player in participants:
                    if player.id in existing_participant_user_ids:
                        continue
                    _ensure_participant(db, tournament.id, player.id)
                    existing_participant_user_ids.add(player.id)
                    created_participants += 1

                winner = participants[(bucket_index + cat_index) % len(participants)]
                tournament.winner_id = winner.id
                db.add(tournament)

                payouts = [
                    (participants[0], Decimal(str(120 + cat_index * 20 + bucket_index * 10))),
                    (participants[1], Decimal(str(80 + cat_index * 15 + bucket_index * 8))),
                    (participants[2], Decimal(str(45 + cat_index * 10 + bucket_index * 5))),
                ]
                for player, amount in payouts:
                    if _ensure_prize_credit(
                        db,
                        user=player,
                        tournament=tournament,
                        category=category,
                        amount=amount,
                        event_at=event_at,
                    ):
                        created_credits += 1

        db.commit()

    print("Leaderboard dummy seed completed")
    print(f"Users created: {created_users}")
    print(f"Tournaments created: {created_tournaments}")
    print(f"Participants created: {created_participants}")
    print(f"Prize credits created: {created_credits}")


if __name__ == "__main__":
    seed_leaderboard_dummy_data()

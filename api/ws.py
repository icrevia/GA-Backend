import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
import logging
from sqlalchemy import select, update

from core.websockets import manager, ALLOWED_WS_EVENTS
from core.security import decode_access_token
from core.database import SessionLocal
from models.user import User
from models.quiz import QuizMatch, QuizResponse, QuizQuestion, QuizParticipant

logger = logging.getLogger("GamerzAdda.ws")
router = APIRouter()


async def _build_quiz_sync_payload(db, quiz_id: int) -> dict | None:
    logger.info(f"Building quiz_sync for quiz_id={quiz_id}")
    quiz_res = await db.execute(select(QuizMatch).where(QuizMatch.id == quiz_id))
    quiz = quiz_res.scalar_one_or_none()
    if not quiz:
        logger.warning(f"Quiz {quiz_id} not found for sync")
        return None
    
    if quiz.status != "LIVE":
        logger.warning(f"Quiz {quiz_id} is not LIVE (status={quiz.status}), skipping sync")
        return None

    q_res = await db.execute(
        select(QuizQuestion)
        .where(QuizQuestion.quiz_id == quiz_id)
        .order_by(QuizQuestion.id.asc())
    )
    all_questions = q_res.scalars().all()
    
    if not all_questions:
        logger.warning(f"No questions found for quiz {quiz_id}!")
        return None

    # Respect admin settings
    questions_per_quiz = quiz.questions_per_quiz if (quiz.questions_per_quiz and quiz.questions_per_quiz > 0) else 10
    time_per_question = quiz.time_per_question if (quiz.time_per_question and quiz.time_per_question > 0) else 5
    questions = all_questions[:questions_per_quiz]

    logger.info(f"Syncing {len(questions)} questions (of {len(all_questions)}) for quiz {quiz_id}, {time_per_question}s each")
    question_pool = []
    for q in questions:
        option_images = list(q.option_images or [])
        options_payload = []
        for idx, opt_text in enumerate(q.options or []):
            image_url = option_images[idx] if idx < len(option_images) else None
            options_payload.append({"text": opt_text, "image_url": image_url})

        question_pool.append({
            "id": q.id,
            "question_text": q.question_text,
            "question_image_url": q.question_image_url,
            "options": options_payload,
            "time_limit": time_per_question,
            "correct_index": q.correct_option_index,  # Sent for client-side feedback
        })

    session_duration = quiz.duration_seconds or max(60, (len(question_pool) * time_per_question) + 30)
    
    # Calculate how many seconds have already elapsed since quiz went LIVE
    # So clients who join mid-quiz can sync to the right question
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    start_time = quiz.start_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=tz.utc)
    elapsed_seconds = int((now - start_time).total_seconds())

    payload = {
        "type": "quiz_sync",
        "quiz_id": quiz_id,
        "questions_per_quiz": len(question_pool),
        "question_pool_size": len(question_pool),
        "time_per_question": time_per_question,
        "duration_seconds": session_duration,
        "elapsed_seconds": elapsed_seconds,
        "question_pool": question_pool,
    }
    logger.info(f"Generated quiz_sync payload for {quiz_id}: {len(question_pool)} questions, elapsed={elapsed_seconds}s")
    return payload



def _extract_ws_token_and_protocol(websocket: WebSocket) -> tuple[str | None, str | None]:
    """
    Extract auth token from websocket handshake without using query parameters.
    Supports:
    - Authorization: Bearer <jwt>
    - Sec-WebSocket-Protocol: GamerzAdda.v1, token.<jwt>
    """
    raw_protocols = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [p.strip() for p in raw_protocols.split(",") if p.strip()]

    selected_protocol = None
    for proto in protocols:
        if proto.lower() == "gamerzadda.v1":
            selected_protocol = "gamerzadda.v1"
            break

    for proto in protocols:
        if proto.lower().startswith("token."):
            token = proto[len("token."):].strip()
            return token or None, selected_protocol

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        return token or None, selected_protocol

    token_qs = websocket.query_params.get("token")
    if token_qs and token_qs not in ("null", "undefined", ""):
        return token_qs.strip(), selected_protocol

    return None, selected_protocol


async def get_user_from_token(token: str) -> tuple[int | None, bool, str | None, int | None, str | None, str | None]:
    """Decode JWT and return (user_id, is_admin, username, mmr, bio, profile_pic)."""
    if not token or token in ("null", "undefined", ""):
        logger.warning("WS Auth: Token is empty or null")
        return None, False, None, None, None, None

    try:
        payload = decode_access_token(token)
    except Exception as e:
        logger.warning(f"WS Auth Token Decode Error: {e}")
        return None, False, None, None, None, None

    user_id = payload.get("sub")
    if user_id is None:
        logger.warning("WS Auth: No 'sub' in token payload")
        return None, False, None, None, None, None

    uid = int(user_id)

    try:
        async with SessionLocal() as db:
            # Keep websocket auth responsive under pool pressure.
            user_row = await asyncio.wait_for(
                db.execute(
                    select(
                        User.id,
                        User.role,
                        User.username,
                        User.is_active,
                        User.token_version,
                        User.mmr,
                        User.bio,
                        User.profile_pic,
                    ).where(User.id == uid)
                ),
                timeout=5,
            )
            row = user_row.first()

        if not row:
            logger.warning(f"WS Auth: user_id={uid} not found in DB")
            return None, False, None, None, None, None

        token_version = payload.get("tv", 0)
        db_token_version = row.token_version or 0
        if not row.is_active:
            logger.warning(f"WS Auth: user_id={uid} is banned")
            return None, False, None, None, None, None

        if int(token_version) != int(db_token_version):
            logger.warning(f"WS Auth: user_id={uid} token version mismatch")
            return None, False, None, None, None, None

        is_admin = (row.role == "ADMIN")
        username = row.username
        mmr = row.mmr or 1200
        bio = row.bio or "Ready for the battle!"
        profile_pic = row.profile_pic
        return uid, is_admin, username, mmr, bio, profile_pic
    except asyncio.TimeoutError:
        logger.warning("WS Auth DB timeout while checking token")
        return None, False, None, None, None, None
    except Exception as e:
        logger.error(f"WS Auth DB Error: {e}")
        return None, False, None, None, None, None


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Log detailed connection metadata to identify client/proxy issues
    headers = dict(websocket.headers)
    logger.info(f"WS Attempt: origin={headers.get('origin')} user_agent={headers.get('user-agent')} protocols={headers.get('sec-websocket-protocol')}")
    
    token, selected_protocol = _extract_ws_token_and_protocol(websocket)

    try:
        # If client sent subprotocols, we MUST select one or None
        # Passing subprotocol=None is safest if we don't strictly enforce versioning at the handshake level.
        await websocket.accept(subprotocol=selected_protocol)
        logger.info(f"WS Accepted: protocol={selected_protocol}")
    except Exception as e:
        logger.error(f"WS Accept Failed: {str(e)}")
        return

    user_id, is_admin, username, mmr, bio, profile_pic = await get_user_from_token(token)
    if not user_id:
        logger.warning("WS rejected: Invalid/Missing token")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Authentication failed. Invalid or missing token."
            }))
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    await manager.connect(user_id, websocket, is_admin=is_admin)

    # Notify the connecting client that they're registered
    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "user_id": user_id,
            "is_admin": is_admin
        }))
    except Exception:
        manager.disconnect(user_id, websocket)
        return

    try:
        while True:
            data = await websocket.receive_text()

            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            logger.info(f"WS Signal: From={user_id} Type={msg_type} IsAdmin={is_admin}")

            if msg_type == "join_quiz":
                quiz_id = int(msg.get("quiz_id", 0))
                if quiz_id:
                    await manager.join_quiz_room(user_id, quiz_id)
                    async with SessionLocal() as db:
                        payload = await _build_quiz_sync_payload(db, quiz_id)
                    if payload:
                        await manager.send_personal_message(payload, user_id)
                continue

            if msg_type == "join_battle":
                entry_fee = int(msg.get("entry_fee", 0))
                from services.quiz_matchmaker import matchmaker
                await matchmaker.add_to_pool(user_id, username, mmr, entry_fee, bio=bio, profile_pic=profile_pic)
                continue

            # Bug #6 fix: cancel_matchmaking had no handler — user stayed in pool forever
            if msg_type == "cancel_matchmaking":
                entry_fee = int(msg.get("entry_fee", 0))
                from services.quiz_matchmaker import matchmaker
                await matchmaker.remove_from_pool(user_id, entry_fee)
                logger.info(f"User {user_id} cancelled matchmaking (fee={entry_fee})")
                continue

            if msg_type == "battle_taunt":
                opponent_id = int(msg.get("opponent_id", 0))
                taunt_id = msg.get("taunt_id", "")
                if opponent_id:
                    await manager.send_personal_message({
                        "type": "battle_taunt",
                        "taunt_id": taunt_id,
                        "from_username": username
                    }, opponent_id)
                continue

            if msg_type == "leave_quiz":
                quiz_id = int(msg.get("quiz_id", 0))
                if quiz_id:
                    await manager.leave_quiz_room(user_id, quiz_id)
                continue

            if msg_type == "quiz_surrender":
                quiz_id = int(msg.get("quiz_id", 0))
                if quiz_id:
                    async with SessionLocal() as db:
                        # Mark the user as surrendered
                        await db.execute(
                            update(QuizParticipant)
                            .where(QuizParticipant.quiz_id == quiz_id, QuizParticipant.user_id == user_id)
                            .values(status="SURRENDERED")
                        )
                        await db.commit()
                        
                        # Immediately process results if it's a battle
                        quiz_res = await db.execute(select(QuizMatch).where(QuizMatch.id == quiz_id))
                        quiz = quiz_res.scalar_one_or_none()
                        if quiz and quiz.match_type == "BATTLE":
                            from services.quiz_orchestrator import orchestrator
                            asyncio.create_task(orchestrator.process_battle_results(quiz_id, surrendered_user_id=user_id))
                continue

            if msg_type == "quiz_answer":
                quiz_id = int(msg.get("quiz_id", 0))
                question_id = int(msg.get("question_id", 0))
                option_index = int(msg.get("option_index", -1))
                response_time = max(0, int(msg.get("response_time_ms", 0)))
                
                if quiz_id and question_id and option_index != -1:
                    async with SessionLocal() as db:
                        # Check if correct
                        q_res = await db.execute(
                            select(QuizQuestion).where(
                                QuizQuestion.id == question_id,
                                QuizQuestion.quiz_id == quiz_id
                            )
                        )
                        question = q_res.scalar_one_or_none()
                        if question:
                            existing_res = await db.execute(
                                select(QuizResponse.id).where(
                                    QuizResponse.quiz_id == quiz_id,
                                    QuizResponse.question_id == question_id,
                                    QuizResponse.user_id == user_id
                                )
                            )
                            if existing_res.scalar_one_or_none() is not None:
                                continue

                            is_correct = (question.correct_option_index == option_index)
                            ans = QuizResponse(
                                quiz_id=quiz_id,
                                question_id=question_id,
                                user_id=user_id,
                                option_index=option_index,
                                is_correct=is_correct,
                                response_time_ms=response_time
                            )
                            db.add(ans)
                            await db.commit()
                continue

            if msg_type == "quiz_complete":
                quiz_id = int(msg.get("quiz_id", 0))
                if quiz_id:
                    async with SessionLocal() as db:
                        await db.execute(
                            update(QuizParticipant)
                            .where(QuizParticipant.quiz_id == quiz_id, QuizParticipant.user_id == user_id)
                            .values(status="COMPLETED")
                        )
                        await db.commit()

                        # Check if everyone is done for BATTLE
                        quiz_res = await db.execute(select(QuizMatch).where(QuizMatch.id == quiz_id))
                        quiz = quiz_res.scalar_one_or_none()

                        if quiz and quiz.match_type == "BATTLE" and quiz.status != "COMPLETED":
                            part_res = await db.execute(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id))
                            all_parts = part_res.scalars().all()
                            if all(p.status in ("COMPLETED", "SURRENDERED") for p in all_parts):
                                # Bug #10 fix: Atomic guard — claim processing right now with a status update.
                                # Only the task that successfully updates status → PROCESSING gets to run results.
                                claim = await db.execute(
                                    update(QuizMatch)
                                    .where(QuizMatch.id == quiz_id, QuizMatch.status == "LIVE")
                                    .values(status="PROCESSING")
                                    .returning(QuizMatch.id)
                                )
                                await db.commit()
                                if claim.scalar_one_or_none() is not None:
                                    from services.quiz_orchestrator import orchestrator
                                    asyncio.create_task(orchestrator.process_battle_results(quiz_id))

                        # Signal client to refresh lobby
                        await manager.send_personal_message({"type": "lobby_refresh"}, user_id)
                continue

            if msg_type == "quiz_sync":
                quiz_id = int(msg.get("quiz_id", 0))
                if quiz_id:
                    async with SessionLocal() as db:
                        payload = await _build_quiz_sync_payload(db, quiz_id)
                    if payload:
                        await manager.send_personal_message(payload, user_id)
                continue

            # (Duplicate quiz_surrender/leave_quiz block removed — Bug #1 fix.
            # quiz_surrender is fully handled above at line 281.
            # leave_quiz is handled at line 275.)

            if msg_type not in ALLOWED_WS_EVENTS:
                logger.debug(f"WS Signal: Unknown type={msg_type} from user_id={user_id}")
                continue

            if is_admin:
                target_user_id = msg.get("to_user_id")
                if not target_user_id:
                    logger.warning(f"WS Admin signal missing to_user_id: type={msg_type}")
                    continue

                target_user_id = int(target_user_id)
                msg["from_user_id"] = user_id
                msg["from"] = "admin"
                msg["from_user_name"] = username or "Admin"

                delivered = await manager.send_personal_message(msg, target_user_id)
                if not delivered:
                    logger.warning(
                        f"WS Message NOT delivered: Admin={user_id} -> User={target_user_id} "
                        f"type={msg_type} (user has no live socket)"
                    )

            else:
                msg["from_user_id"] = user_id
                msg["from_user_name"] = username or f"User #{user_id}"

                await manager.broadcast_to_admins(msg)

    except WebSocketDisconnect:
        # Check if user was in an active BATTLE before disconnecting
        async with SessionLocal() as db:
            # Find all quiz rooms user was in
            for q_id in list(manager.quiz_rooms.keys()):
                if user_id in manager.quiz_rooms[q_id]:
                    # Check if this quiz is a LIVE BATTLE
                    quiz_res = await db.execute(select(QuizMatch).where(QuizMatch.id == q_id, QuizMatch.status == "LIVE", QuizMatch.match_type == "BATTLE"))
                    quiz = quiz_res.scalar_one_or_none()
                    if quiz:
                        logger.info(f"User {user_id} disconnected during active BATTLE {q_id}. Auto-surrendering.")
                        await db.execute(
                            update(QuizParticipant)
                            .where(QuizParticipant.quiz_id == q_id, QuizParticipant.user_id == user_id)
                            .values(status="SURRENDERED")
                        )
                        await db.commit()
                        from services.quiz_orchestrator import orchestrator
                        asyncio.create_task(orchestrator.process_battle_results(q_id, surrendered_user_id=user_id))
        
        manager.disconnect(user_id, websocket)
    except Exception as e:
        logger.warning(f"WS runtime error for user_id={user_id}: {e}")
        manager.disconnect(user_id, websocket)

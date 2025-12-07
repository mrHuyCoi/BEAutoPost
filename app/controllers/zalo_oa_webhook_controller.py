from __future__ import annotations
import hmac
import hashlib
import json
import logging
from typing import Optional, Dict, Any
import re
import unicodedata

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx

from app.database.database import get_db
from app.models.oa_webhook_event import OaWebhookEvent
from app.models.oa_message import OaMessage
from app.configs.settings import settings
from app.models.oa_account import OaAccount
from app.models.oa_token import OaToken
from app.models.oa_blocked_user import OaBlockedUser
from app.repositories.user_api_key_repository import UserApiKeyRepository
from app.services.zalo_oa_service import ZaloOAService

router = APIRouter()

logger = logging.getLogger(__name__)


async def _push_message_to_websocket(
    owner_user_id: str,
    oa_id: str,
    partner_id: str,
    message_data: dict
) -> None:
    """
    Push tin nhắn Zalo OA mới qua WebSocket để frontend nhận real-time.
    Không block webhook nếu WebSocket fail.
    
    Args:
        owner_user_id: ID của user sở hữu OA account
        oa_id: Zalo OA ID
        partner_id: ID của người gửi/nhận (conversation_id)
        message_data: Dict chứa thông tin tin nhắn (id, message_id, text, direction, timestamp, etc.)
    """
    try:
        from app.controllers.websocket_controller import manager
        
        ws_message = {
            "type": "new_message",
            "event": "zalo_oa_message_received",
            "oa_id": oa_id,
            "partner_id": partner_id,
            "conversation_id": partner_id,  # partner_id là conversation_id trong Zalo OA
            "message": message_data,
        }
        
        # Log chi tiết để debug
        logger.info(
            f"📨 Pushing Zalo OA message to WebSocket: user_id={owner_user_id}, oa_id={oa_id}, "
            f"partner_id={partner_id}, direction={message_data.get('direction')}, "
            f"text={message_data.get('text', '')[:50] if message_data.get('text') else 'N/A'}..."
        )
        logger.debug(f"WebSocket message payload: {json.dumps(ws_message, ensure_ascii=False)}")
        
        await manager.broadcast_to_user(
            str(owner_user_id),
            json.dumps(ws_message, ensure_ascii=False)
        )
        logger.info(f"✅ Successfully pushed Zalo OA message to WebSocket for user_id={owner_user_id}")
    except Exception as ws_error:
        # Log lỗi nhưng không block webhook processing
        logger.error(f"❌ Failed to push Zalo OA message via WebSocket: {ws_error}", exc_info=True)


def verify_signature(body_bytes: bytes, signature: Optional[str], headers: Optional[Dict[str, Any]] = None) -> bool:
    # Prefer OA secret key per Zalo spec; fallback to webhook secret if provided
    secret = settings.ZALO_OA_WEBHOOK_SECRET
    if not secret:
        return True  # Signature not configured, skip
    if not signature:
        return False

    # Extract signature value (Zalo sends like: "mac=<hex>")
    sig_value = signature.strip()
    if sig_value.lower().startswith("mac="):
        sig_value = sig_value.split("=", 1)[1].strip()
    sig_value = sig_value.lower()

    # Parse payload to get app_id and timestamp
    # Zalo formula: sha256(appId + data + timestamp + OAsecretKey)
    try:
        data_str = body_bytes.decode("utf-8")
        payload = json.loads(data_str) if data_str else {}

        # app_id from payload, fallback to configured app id
        app_id = str(payload.get("app_id") or settings.ZALO_OA_APP_ID or "")

        # timestamp variations that Zalo might use (body + potential headers)
        body_ts_val = (
            payload.get("timestamp")
            or payload.get("timeStamp")
            or payload.get("ts")
            or payload.get("time_stamp")
            or payload.get("timestamp_ms")
            or None
        )
        header_ts_val = None
        ts_source = "body"
        if headers:
            try:
                h_lc = {str(k).lower(): v for k, v in headers.items()}
                for hk in [
                    "x-zevent-timestamp",
                    "x-request-timestamp",
                    "x-zrequest-timestamp",
                    "x-time",
                    "x-timestamp",
                ]:
                    if hk in h_lc and h_lc[hk]:
                        header_ts_val = h_lc[hk]
                        break
            except Exception:
                header_ts_val = None
        ts_val = body_ts_val or header_ts_val or ""
        ts_source = "body" if body_ts_val is not None else ("header" if header_ts_val is not None else "none")
        timestamp = str(ts_val)

        # Compute expected from raw body
        to_hash_raw = app_id + data_str + timestamp + secret
        expected_raw = hashlib.sha256(to_hash_raw.encode("utf-8")).hexdigest()

        if hmac.compare_digest(expected_raw, sig_value):
            return True

        # Try with minified JSON (no spaces) in case of body formatting differences
        try:
            data_min = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            to_hash_min = app_id + data_min + timestamp + secret
            expected_min = hashlib.sha256(to_hash_min.encode("utf-8")).hexdigest()
        except Exception:
            data_min = None
            expected_min = None

        matched_min = expected_min is not None and hmac.compare_digest(expected_min, sig_value)
        if not matched_min:
            logger.warning(
                "Zalo signature mismatch: app_id=%s timestamp=%s (src=%s) secret=%s body_len=%d provided=%s expected_raw=%s expected_min=%s",
                app_id,
                timestamp,
                ts_source,
                "ZALO_OA_SECRET_KEY" if settings.ZALO_OA_SECRET_KEY else "ZALO_OA_WEBHOOK_SECRET",
                len(body_bytes),
                sig_value[:16] if sig_value else None,
                (expected_raw[:16] if expected_raw else None),
                (expected_min[:16] if expected_min else None),
            )
        return matched_min
    except Exception:
        logger.exception("verify_signature exception")
        return False


@router.post("/webhook")
async def zalo_oa_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_zevent_signature: Optional[str] = Header(default=None, convert_underscores=True),
):
    body_bytes = await request.body()
    if not verify_signature(body_bytes, x_zevent_signature, headers=dict(request.headers)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        payload = {}

    # Dedup key best-effort
    dedupe_id = None
    try:
        dedupe_id = (
            payload.get("event_id")
            or payload.get("message", {}).get("msg_id")
            or payload.get("msg_id")
        )
    except Exception:
        pass

    event = OaWebhookEvent(
        event_type=((payload.get("event_name") or payload.get("event")) if isinstance(payload, dict) else None),
        dedupe_id=str(dedupe_id) if dedupe_id else None,
        payload=payload,
    )
    # Dedupe: if dedupe_id exists, skip insert
    if event.dedupe_id:
        try:
            res = await db.execute(select(OaWebhookEvent.id).where(OaWebhookEvent.dedupe_id == event.dedupe_id))
            if res.scalar() is not None:
                return {"ok": True, "deduped": True}
        except Exception:
            # best-effort dedupe; continue to insert
            pass

    db.add(event)
    # Flush to catch unique violations here (so we can rollback and return 200)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"ok": True, "deduped": True}

    # Business logic: route events
    try:
        evt_name = (payload.get("event_name") or payload.get("event") or "") if isinstance(payload, dict) else ""
        sender_id = str((payload.get("sender") or {}).get("id")) if isinstance(payload, dict) else None
        recipient_id = str((payload.get("recipient") or {}).get("id")) if isinstance(payload, dict) else None
        msg_obj = (payload.get("message") or {}) if isinstance(payload, dict) else {}
        text = _extract_message_text(payload)
        msg_id = _extract_message_id(payload)

        # Resolve OA account and partner_id based on event
        oa_id: Optional[str] = None
        partner_id: Optional[str] = None
        if evt_name == "user_send_text":
            # Inbound from user -> OA
            oa_id = recipient_id
            partner_id = sender_id
        elif evt_name == "user_send_image":
            # Inbound image from user -> OA
            oa_id = recipient_id
            partner_id = sender_id
        elif evt_name == "user_received_message":
            # Outbound from OA -> user (self-sent message or echo)
            oa_id = sender_id
            partner_id = recipient_id
        else:
            # Unhandled event types are acknowledged
            return {"ok": True, "ignored": True}

        if not oa_id or not partner_id:
            return {"ok": True, "ignored": True}

        account = await _get_owner_by_oa_id(db, oa_id)
        if not account:
            return {"ok": True, "unknown_oa": True}

        owner_user_id = str(account.owner_user_id)

        # Check if partner is blocked for this OA account
        try:
            blk = await db.execute(
                select(OaBlockedUser.id).where(
                    (OaBlockedUser.oa_account_id == account.id) & (OaBlockedUser.blocked_user_id == partner_id)
                )
            )
            if blk.scalar() is not None:
                logger.info(
                    "ZaloOA webhook: blocked partner -> skip; owner_user_id=%s oa_id=%s partner_id=%s",
                    owner_user_id, oa_id, partner_id,
                )
                return {"ok": True, "blocked": True}
        except Exception:
            # On error checking blocked list, proceed to avoid blocking webhook
            pass

        # Outbound (OA -> user): pause only if NOT sent by our chatbot recently
        if evt_name == "user_received_message":
            # Try message-id based match first (more reliable), then text-based
            hit_id = bool(msg_id and _bot_is_recently_sent_id(owner_user_id, oa_id, partner_id, str(msg_id)))
            if hit_id:
                logger.info(
                    "ZaloOA webhook: outbound from bot (id-match) -> no pause; owner_user_id=%s oa_id=%s partner_id=%s msg_id=%s",
                    owner_user_id, oa_id, partner_id, msg_id,
                )
                # Still save and push outbound message from bot
                oa_msg = None
                try:
                    if msg_id:
                        existing = await db.execute(
                            select(OaMessage).where(OaMessage.message_id_from_zalo == str(msg_id))
                        )
                        if existing.scalar_one_or_none() is not None:
                            oa_msg = existing.scalar_one()
                        else:
                            timestamp = _now_vn_naive()
                            try:
                                ts_val = payload.get("timestamp") or payload.get("timeStamp") or payload.get("ts")
                                if ts_val:
                                    if isinstance(ts_val, (int, float)):
                                        if ts_val > 1e10:
                                            timestamp = datetime.fromtimestamp(ts_val / 1000, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                        else:
                                            timestamp = datetime.fromtimestamp(ts_val, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                    elif isinstance(ts_val, str):
                                        timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                            except Exception:
                                pass
                            oa_msg = OaMessage(
                                oa_account_id=account.id,
                                conversation_id=partner_id,
                                direction="out",
                                msg_type="text" if text else None,
                                text=text,
                                message_id_from_zalo=str(msg_id),
                                timestamp=timestamp,
                                raw_payload=payload,
                            )
                            db.add(oa_msg)
                            await db.commit()
                except IntegrityError:
                    await db.rollback()
                except Exception as db_error:
                    await db.rollback()
                    logger.error(f"Failed to save outbound message: {db_error}", exc_info=True)
                
                if oa_msg:
                    await _push_message_to_websocket(
                        owner_user_id,
                        oa_id,
                        partner_id,
                        {
                            "id": str(oa_msg.id),
                            "message_id": str(msg_id) if msg_id else None,
                            "text": text,
                            "direction": "out",
                            "msg_type": "text" if text else None,
                            "timestamp": oa_msg.timestamp.isoformat() if oa_msg.timestamp else None,
                            "raw_payload": payload,
                        }
                    )
                return {"ok": True, "from_bot": True, "match": "id"}
            hit_text = bool(text and _bot_is_recently_sent(owner_user_id, oa_id, partner_id, str(text)))
            if hit_text:
                # Outbound echo of our chatbot message -> do not pause
                logger.info(
                    "ZaloOA webhook: outbound from bot (text-match) -> no pause; owner_user_id=%s oa_id=%s partner_id=%s",
                    owner_user_id, oa_id, partner_id,
                )
                # Still save and push outbound message from bot
                oa_msg = None
                try:
                    if msg_id:
                        existing = await db.execute(
                            select(OaMessage).where(OaMessage.message_id_from_zalo == str(msg_id))
                        )
                        if existing.scalar_one_or_none() is not None:
                            oa_msg = existing.scalar_one()
                    if not oa_msg:
                        timestamp = _now_vn_naive()
                        try:
                            ts_val = payload.get("timestamp") or payload.get("timeStamp") or payload.get("ts")
                            if ts_val:
                                if isinstance(ts_val, (int, float)):
                                    if ts_val > 1e10:
                                        timestamp = datetime.fromtimestamp(ts_val / 1000, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                    else:
                                        timestamp = datetime.fromtimestamp(ts_val, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                elif isinstance(ts_val, str):
                                    timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                        except Exception:
                            pass
                        oa_msg = OaMessage(
                            oa_account_id=account.id,
                            conversation_id=partner_id,
                            direction="out",
                            msg_type="text" if text else None,
                            text=text,
                            message_id_from_zalo=str(msg_id) if msg_id else None,
                            timestamp=timestamp,
                            raw_payload=payload,
                        )
                        db.add(oa_msg)
                        await db.commit()
                except IntegrityError:
                    await db.rollback()
                except Exception as db_error:
                    await db.rollback()
                    logger.error(f"Failed to save outbound message: {db_error}", exc_info=True)
                
                if oa_msg:
                    await _push_message_to_websocket(
                        owner_user_id,
                        oa_id,
                        partner_id,
                        {
                            "id": str(oa_msg.id),
                            "message_id": str(msg_id) if msg_id else None,
                            "text": text,
                            "direction": "out",
                            "msg_type": "text" if text else None,
                            "timestamp": oa_msg.timestamp.isoformat() if oa_msg.timestamp else None,
                            "raw_payload": payload,
                        }
                    )
                return {"ok": True, "from_bot": True, "match": "text"}
            # Extra debug to understand mismatches
            try:
                logger.info(
                    "ZaloOA webhook: outbound not matched; owner_user_id=%s oa_id=%s partner_id=%s msg_id=%s text_norm_snippet=%s",
                    owner_user_id, oa_id, partner_id, (msg_id or None), (_norm_text(text)[:120] if text else None),
                )
            except Exception:
                pass
            # Pause due to outbound likely from human owner; record until
            until = _now_vn_naive() + timedelta(minutes=10)
            _set_paused(owner_user_id, oa_id, partner_id, minutes=10)
            logger.info(
                "ZaloOA webhook: paused due to outbound; owner_user_id=%s oa_id=%s partner_id=%s until=%s",
                owner_user_id, oa_id, partner_id, until.isoformat(),
            )
            # Still save and push outbound message (from human)
            oa_msg = None
            try:
                if msg_id:
                    existing = await db.execute(
                        select(OaMessage).where(OaMessage.message_id_from_zalo == str(msg_id))
                    )
                    existing_msg = existing.scalar_one_or_none()
                    if existing_msg is not None:
                        oa_msg = existing_msg
                if not oa_msg:
                    timestamp = _now_vn_naive()
                    try:
                        ts_val = payload.get("timestamp") or payload.get("timeStamp") or payload.get("ts")
                        if ts_val:
                            if isinstance(ts_val, (int, float)):
                                if ts_val > 1e10:
                                    timestamp = datetime.fromtimestamp(ts_val / 1000, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                else:
                                    timestamp = datetime.fromtimestamp(ts_val, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                            elif isinstance(ts_val, str):
                                timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                    except Exception:
                        pass
                    oa_msg = OaMessage(
                        oa_account_id=account.id,
                        conversation_id=partner_id,
                        direction="out",
                        msg_type="text" if text else None,
                        text=text,
                        message_id_from_zalo=str(msg_id) if msg_id else None,
                        timestamp=timestamp,
                        raw_payload=payload,
                    )
                    db.add(oa_msg)
                    await db.commit()
            except IntegrityError:
                await db.rollback()
            except Exception as db_error:
                await db.rollback()
                logger.error(f"Failed to save outbound message: {db_error}", exc_info=True)
            
            if oa_msg:
                await _push_message_to_websocket(
                    owner_user_id,
                    oa_id,
                    partner_id,
                    {
                        "id": str(oa_msg.id),
                        "message_id": str(msg_id) if msg_id else None,
                        "text": text,
                        "direction": "out",
                        "msg_type": "text" if text else None,
                        "timestamp": oa_msg.timestamp.isoformat() if oa_msg.timestamp else None,
                        "raw_payload": payload,
                    }
                )
            return {"ok": True, "paused": 10, "until": until.isoformat()}

        # Inbound user message (text)
        if evt_name == "user_send_text":
            if not text or not isinstance(text, str) or not text.strip():
                return {"ok": True}

            # Check paused state
            if _is_paused(owner_user_id, oa_id, partner_id):
                pu = _get_pause_until(owner_user_id, oa_id, partner_id)
                logger.info(
                    "ZaloOA webhook: inbound skipped due to paused; owner_user_id=%s oa_id=%s partner_id=%s until=%s",
                    owner_user_id, oa_id, partner_id, (pu.isoformat() if pu else None),
                )
                return {"ok": True, "paused": True, "until": (pu.isoformat() if pu else None)}

            # Save inbound message to database (if not duplicate)
            oa_msg = None
            try:
                if msg_id:
                    # Check for duplicate by message_id_from_zalo
                    existing = await db.execute(
                        select(OaMessage).where(OaMessage.message_id_from_zalo == str(msg_id))
                    )
                    if existing.scalar_one_or_none() is not None:
                        logger.debug(f"Duplicate Zalo OA message detected (msg_id={msg_id}), skipping DB insert")
                    else:
                        # Extract timestamp from payload
                        timestamp = None
                        try:
                            ts_val = payload.get("timestamp") or payload.get("timeStamp") or payload.get("ts")
                            if ts_val:
                                if isinstance(ts_val, (int, float)):
                                    # Assume milliseconds if large number
                                    if ts_val > 1e10:
                                        timestamp = datetime.fromtimestamp(ts_val / 1000, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                    else:
                                        timestamp = datetime.fromtimestamp(ts_val, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                elif isinstance(ts_val, str):
                                    # Try parsing ISO format
                                    timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                        except Exception:
                            timestamp = _now_vn_naive()

                        oa_msg = OaMessage(
                            oa_account_id=account.id,
                            conversation_id=partner_id,
                            direction="in",
                            msg_type="text",
                            text=text,
                            message_id_from_zalo=str(msg_id) if msg_id else None,
                            timestamp=timestamp or _now_vn_naive(),
                            raw_payload=payload,
                        )
                        db.add(oa_msg)
                        await db.commit()
                        logger.info(
                            f"💾 Zalo OA message saved to database: id={oa_msg.id}, oa_id={oa_id}, "
                            f"partner_id={partner_id}, msg_id={msg_id}"
                        )
                else:
                    # No msg_id, create without unique constraint
                    timestamp = _now_vn_naive()
                    oa_msg = OaMessage(
                        oa_account_id=account.id,
                        conversation_id=partner_id,
                        direction="in",
                        msg_type="text",
                        text=text,
                        message_id_from_zalo=None,
                        timestamp=timestamp,
                        raw_payload=payload,
                    )
                    db.add(oa_msg)
                    await db.commit()
                    logger.info(
                        f"💾 Zalo OA message saved to database (no msg_id): id={oa_msg.id}, oa_id={oa_id}, "
                        f"partner_id={partner_id}"
                    )
            except IntegrityError:
                await db.rollback()
                logger.debug(f"Duplicate Zalo OA message (integrity error), skipping")
            except Exception as db_error:
                await db.rollback()
                logger.error(f"Failed to save Zalo OA message to database: {db_error}", exc_info=True)
                # Continue processing even if DB save fails

            # Push message to WebSocket
            if oa_msg:
                await _push_message_to_websocket(
                    owner_user_id,
                    oa_id,
                    partner_id,
                    {
                        "id": str(oa_msg.id),
                        "message_id": str(msg_id) if msg_id else None,
                        "text": text,
                        "direction": "in",
                        "msg_type": "text",
                        "timestamp": oa_msg.timestamp.isoformat() if oa_msg.timestamp else None,
                        "raw_payload": payload,
                    }
                )

            # Get user's API key for X-API-Key auth
            api_key = await _get_user_api_key(db, account.owner_user_id)
            if not api_key:
                return {"ok": True, "no_api_key": True}

            # Decide which chatbot to use: check mobile first, then custom
            use_mobile = True
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    mobile_url = f"{settings.CHATBOT_API_BASE_URL}/customer/status/{owner_user_id}"
                    r1 = await client.get(mobile_url)
                    mobile_status = r1.json() if r1.status_code == 200 else None
                    if _is_stopped_status(mobile_status):
                        use_mobile = False
            except Exception:
                # If status check fails, default to mobile
                use_mobile = True

            # Build base URL of this server to call internal chatbot endpoints with X-API-Key
            try:
                host = (request.headers.get("host") or (
                    f"{request.url.hostname}:{request.url.port}" if getattr(request.url, "port", None) else str(request.url.hostname)
                ))
                base_url = f"{request.url.scheme}://{host}"
            except Exception:
                base_url = ""

            reply: Optional[str] = None

            if use_mobile:
                reply = await _call_mobile_chatbot(base_url, api_key, partner_id, text.strip())
            else:
                # Check custom status
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        custom_url = f"{settings.CHATBOT_CUSTOM_API_BASE_URL}/bot-status/{owner_user_id}"
                        r2 = await client.get(custom_url)
                        custom_status = r2.json() if r2.status_code == 200 else None
                        if not _is_stopped_status(custom_status):
                            reply = await _call_custom_chatbot(base_url, api_key, partner_id, text.strip())
                except Exception:
                    pass

            if reply:
                await _send_zalo_reply(db, account, owner_user_id, oa_id, partner_id, reply)

        # Inbound user message (image)
        elif evt_name == "user_send_image":
            img_url = _extract_first_image_url(payload)
            # If image URL not present, nothing to do
            if not img_url:
                return {"ok": True}

            # Check paused state
            if _is_paused(owner_user_id, oa_id, partner_id):
                pu = _get_pause_until(owner_user_id, oa_id, partner_id)
                try:
                    logger.info(
                        "ZaloOA webhook: inbound image skipped due to paused; owner_user_id=%s oa_id=%s partner_id=%s until=%s",
                        owner_user_id, oa_id, partner_id, (pu.isoformat() if pu else None),
                    )
                except Exception:
                    pass
                return {"ok": True, "paused": True, "until": (pu.isoformat() if pu else None)}

            # Save inbound image message to database (if not duplicate)
            oa_msg = None
            try:
                if msg_id:
                    # Check for duplicate by message_id_from_zalo
                    existing = await db.execute(
                        select(OaMessage).where(OaMessage.message_id_from_zalo == str(msg_id))
                    )
                    if existing.scalar_one_or_none() is not None:
                        logger.debug(f"Duplicate Zalo OA image message detected (msg_id={msg_id}), skipping DB insert")
                    else:
                        # Extract timestamp from payload
                        timestamp = None
                        try:
                            ts_val = payload.get("timestamp") or payload.get("timeStamp") or payload.get("ts")
                            if ts_val:
                                if isinstance(ts_val, (int, float)):
                                    if ts_val > 1e10:
                                        timestamp = datetime.fromtimestamp(ts_val / 1000, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                    else:
                                        timestamp = datetime.fromtimestamp(ts_val, tz=ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                                elif isinstance(ts_val, str):
                                    timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
                        except Exception:
                            timestamp = _now_vn_naive()

                        attachments = [{"type": "image", "url": img_url}]
                        oa_msg = OaMessage(
                            oa_account_id=account.id,
                            conversation_id=partner_id,
                            direction="in",
                            msg_type="image",
                            text=msg_text if msg_text else None,
                            attachments=attachments,
                            message_id_from_zalo=str(msg_id) if msg_id else None,
                            timestamp=timestamp or _now_vn_naive(),
                            raw_payload=payload,
                        )
                        db.add(oa_msg)
                        await db.commit()
                        logger.info(
                            f"💾 Zalo OA image message saved to database: id={oa_msg.id}, oa_id={oa_id}, "
                            f"partner_id={partner_id}, msg_id={msg_id}"
                        )
                else:
                    # No msg_id, create without unique constraint
                    timestamp = _now_vn_naive()
                    attachments = [{"type": "image", "url": img_url}]
                    oa_msg = OaMessage(
                        oa_account_id=account.id,
                        conversation_id=partner_id,
                        direction="in",
                        msg_type="image",
                        text=msg_text if msg_text else None,
                        attachments=attachments,
                        message_id_from_zalo=None,
                        timestamp=timestamp,
                        raw_payload=payload,
                    )
                    db.add(oa_msg)
                    await db.commit()
                    logger.info(
                        f"💾 Zalo OA image message saved to database (no msg_id): id={oa_msg.id}, oa_id={oa_id}, "
                        f"partner_id={partner_id}"
                    )
            except IntegrityError:
                await db.rollback()
                logger.debug(f"Duplicate Zalo OA image message (integrity error), skipping")
            except Exception as db_error:
                await db.rollback()
                logger.error(f"Failed to save Zalo OA image message to database: {db_error}", exc_info=True)
                # Continue processing even if DB save fails

            # Push message to WebSocket
            if oa_msg:
                await _push_message_to_websocket(
                    owner_user_id,
                    oa_id,
                    partner_id,
                    {
                        "id": str(oa_msg.id),
                        "message_id": str(msg_id) if msg_id else None,
                        "text": msg_text if msg_text else None,
                        "direction": "in",
                        "msg_type": "image",
                        "attachments": oa_msg.attachments,
                        "timestamp": oa_msg.timestamp.isoformat() if oa_msg.timestamp else None,
                        "raw_payload": payload,
                    }
                )

            # Get user's API key for X-API-Key auth
            api_key = await _get_user_api_key(db, account.owner_user_id)
            if not api_key:
                return {"ok": True, "no_api_key": True}

            # Decide which chatbot to use: check mobile first, then custom
            use_mobile = True
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    mobile_url = f"{settings.CHATBOT_API_BASE_URL}/customer/status/{owner_user_id}"
                    r1 = await client.get(mobile_url)
                    mobile_status = r1.json() if r1.status_code == 200 else None
                    if _is_stopped_status(mobile_status):
                        use_mobile = False
            except Exception:
                use_mobile = True

            # Build base URL
            try:
                host = (request.headers.get("host") or (
                    f"{request.url.hostname}:{request.url.port}" if getattr(request.url, "port", None) else str(request.url.hostname)
                ))
                base_url = f"{request.url.scheme}://{host}"
            except Exception:
                base_url = ""

            reply: Optional[str] = None
            msg_text = (text.strip() if isinstance(text, str) else "") or ""

            if use_mobile:
                reply = await _call_mobile_chatbot(base_url, api_key, partner_id, msg_text, image_url=img_url)
            else:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        custom_url = f"{settings.CHATBOT_CUSTOM_API_BASE_URL}/bot-status/{owner_user_id}"
                        r2 = await client.get(custom_url)
                        custom_status = r2.json() if r2.status_code == 200 else None
                        if not _is_stopped_status(custom_status):
                            reply = await _call_custom_chatbot(base_url, api_key, partner_id, msg_text, image_url=img_url)
                except Exception:
                    pass

            if reply:
                await _send_zalo_reply(db, account, owner_user_id, oa_id, partner_id, reply)

    except Exception as e:
        logger.exception(f"Failed to handle Zalo webhook event: {e}")
        # best-effort: never break webhook contract
        pass

    return {"ok": True}


# ---------------- Conversation pause (in-memory TTL) ----------------
_PAUSE_MAP: dict[tuple[str, str, str], datetime] = {}

def _now_vn_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)

def _pause_key(owner_user_id: str, oa_id: str, partner_id: str) -> tuple[str, str, str]:
    return (str(owner_user_id), str(oa_id), str(partner_id))

def _set_paused(owner_user_id: str, oa_id: str, partner_id: str, minutes: int = 10):
    if minutes <= 0:
        return
    _PAUSE_MAP[_pause_key(owner_user_id, oa_id, partner_id)] = _now_vn_naive() + timedelta(minutes=minutes)

def _is_paused(owner_user_id: str, oa_id: str, partner_id: str) -> bool:
    key = _pause_key(owner_user_id, oa_id, partner_id)
    until = _PAUSE_MAP.get(key)
    if not until:
        return False
    now = _now_vn_naive()
    if until > now:
        return True
    # expired -> clear
    _PAUSE_MAP.pop(key, None)
    return False

def _get_pause_until(owner_user_id: str, oa_id: str, partner_id: str) -> datetime | None:
    key = _pause_key(owner_user_id, oa_id, partner_id)
    until = _PAUSE_MAP.get(key)
    if not until:
        return None
    now = _now_vn_naive()
    if until > now:
        return until
    # expired -> clear
    _PAUSE_MAP.pop(key, None)
    return None


# ---------------- Bot-sent messages cache (in-memory TTL) ----------------
_BOT_SENT_CACHE: dict[tuple[str, str, str, str], datetime] = {}
_BOT_SENT_ID_CACHE: dict[tuple[str, str, str, str], datetime] = {}

def _norm_text(t: str) -> str:
    """Normalize text for reliable comparison across send/echo.
    - Unicode normalize (NFC)
    - Remove control/format characters (e.g., zero-width)
    - Collapse whitespace and trim
    """
    try:
        s = unicodedata.normalize("NFC", str(t))
        s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Cc", "Cs"))
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = re.sub(r"\s+", " ", s).strip()
        return s
    except Exception:
        try:
            return str(t).strip()
        except Exception:
            return ""

def _bot_key(owner_user_id: str, oa_id: str, partner_id: str, text: str) -> tuple[str, str, str, str]:
    return (str(owner_user_id), str(oa_id), str(partner_id), _norm_text(text or ""))

def _bot_mark_sent(owner_user_id: str, oa_id: str, partner_id: str, text: str, ttl_minutes: int = 5) -> None:
    norm = _norm_text(text)
    if ttl_minutes <= 0 or not norm:
        return
    _BOT_SENT_CACHE[_bot_key(owner_user_id, oa_id, partner_id, norm)] = _now_vn_naive() + timedelta(minutes=ttl_minutes)

def _bot_is_recently_sent(owner_user_id: str, oa_id: str, partner_id: str, text: str) -> bool:
    norm = _norm_text(text)
    if not norm:
        return False
    key = _bot_key(owner_user_id, oa_id, partner_id, norm)
    until = _BOT_SENT_CACHE.get(key)
    if not until:
        return False
    now = _now_vn_naive()
    if until > now:
        return True
    # expired -> clear
    _BOT_SENT_CACHE.pop(key, None)
    return False

def _bot_id_key(owner_user_id: str, oa_id: str, partner_id: str, msg_id: str) -> tuple[str, str, str, str]:
    return (str(owner_user_id), str(oa_id), str(partner_id), str(msg_id or ""))

def _bot_mark_sent_id(owner_user_id: str, oa_id: str, partner_id: str, msg_id: str, ttl_minutes: int = 5) -> None:
    if ttl_minutes <= 0 or not msg_id:
        return
    _BOT_SENT_ID_CACHE[_bot_id_key(owner_user_id, oa_id, partner_id, msg_id)] = _now_vn_naive() + timedelta(minutes=ttl_minutes)

def _bot_is_recently_sent_id(owner_user_id: str, oa_id: str, partner_id: str, msg_id: str) -> bool:
    if not msg_id:
        return False
    key = _bot_id_key(owner_user_id, oa_id, partner_id, msg_id)
    until = _BOT_SENT_ID_CACHE.get(key)
    if not until:
        return False
    now = _now_vn_naive()
    if until > now:
        return True
    _BOT_SENT_ID_CACHE.pop(key, None)
    return False


async def _get_owner_by_oa_id(db: AsyncSession, oa_id: str) -> OaAccount | None:
    res = await db.execute(select(OaAccount).where(OaAccount.oa_id == str(oa_id)))
    return res.scalars().first()


def _is_stopped_status(obj: Any) -> bool:
    try:
        if obj is None:
            return False
        # Common fields: is_stopped / stopped / status contains 'stop'
        if isinstance(obj, dict):
            for k in ["is_stopped", "stopped", "stop", "paused"]:
                if k in obj and bool(obj.get(k)):
                    return True
            st = obj.get("status") or obj.get("state") or obj.get("message")
            if isinstance(st, str) and ("stop" in st.lower() or "pause" in st.lower()):
                return True
        return False
    except Exception:
        return False


def _extract_message_text(payload: Any) -> str | None:
    try:
        if not isinstance(payload, dict):
            return None
        m = payload.get("message") or {}
        candidates = [
            (m.get("text") if isinstance(m, dict) else None),
            (m.get("msg_text") if isinstance(m, dict) else None),
            payload.get("text"),
            payload.get("content"),
            (m.get("message") if isinstance(m, dict) else None),
            (m.get("content") if isinstance(m, dict) else None),
        ]
        for c in candidates:
            if isinstance(c, str) and c.strip():
                return c
        return None
    except Exception:
        return None

def _extract_first_image_url(payload: Any) -> str | None:
    try:
        if not isinstance(payload, dict):
            return None
        m = payload.get("message") or {}
        atts = m.get("attachments") if isinstance(m, dict) else None
        if not isinstance(atts, list):
            return None
        for att in atts:
            if not isinstance(att, dict):
                continue
            t = str(att.get("type") or "").lower()
            if t != "image":
                continue
            p = att.get("payload") or {}
            if isinstance(p, dict):
                url = (
                    p.get("url")
                    or p.get("href")
                    or p.get("thumbnail")
                    or p.get("thumb")
                )
                if isinstance(url, str) and url.strip():
                    return url
        return None
    except Exception:
        return None

def _extract_message_id(payload: Any) -> str | None:
    try:
        if not isinstance(payload, dict):
            return None
        m = payload.get("message") or {}
        for k in [
            "msg_id", "message_id", "msgId", "messageId",
        ]:
            v = None
            if isinstance(m, dict):
                v = m.get(k)
            if not v:
                v = payload.get(k)
            if isinstance(v, (str, int)):
                return str(v)
        return None
    except Exception:
        return None


async def _call_mobile_chatbot(self_base_url: str, api_key: str, thread_id: str, text: str, image_url: Optional[str] = None, image_base64: Optional[str] = None) -> str | None:
    url = f"{self_base_url}/api/v1/chatbot/chat"
    payload = {
        "query": text,
        "llm_provider": "google_genai",
        "thread_id": thread_id,
        "platform": "zalo_oa",
    }
    if image_url:
        payload["image_url"] = image_url
    if image_base64:
        payload["image_base64"] = image_base64
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                return None
            data = r.json()
            # ResponseModel.success wraps data
            body = data.get("data") if isinstance(data, dict) else None
            if isinstance(body, dict):
                # ChatbotMobile typically returns {"response": "..."}
                return body.get("response") or body.get("reply") or body.get("message")
            return None
    except Exception:
        return None


async def _call_custom_chatbot(self_base_url: str, api_key: str, thread_id: str, text: str, image_url: Optional[str] = None) -> str | None:
    url = f"{self_base_url}/api/v1/chatbot-linhkien/chat"
    form = {
        "message": text,
        "model_choice": "gemini",
        "session_id": thread_id,
        "platform": "zalo_oa",
    }
    if image_url:
        form["image_url"] = image_url
    headers = {"X-API-Key": api_key}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, data=form, headers=headers)
            if r.status_code != 200:
                return None
            data = r.json()
            # ChatbotCustom returns raw JSON, prefer 'reply'
            if isinstance(data, dict):
                return data.get("reply") or data.get("response") or data.get("message")
            return None
    except Exception:
        return None


async def _send_zalo_reply(db: AsyncSession, account: OaAccount, owner_user_id: str, oa_id: str, to_user_id: str, text: str) -> None:
    if not text:
        return
    res = await db.execute(select(OaToken).where(OaToken.oa_account_id == account.id).order_by(desc(OaToken.created_at)))
    token = res.scalars().first()
    if not token:
        return
    try:
        # Pre-mark to handle extremely fast webhook echo
        _bot_mark_sent(owner_user_id, oa_id, to_user_id, text, ttl_minutes=5)
        result = await ZaloOAService.send_text_message(token.access_token, to_user_id, text)
        # Mark this message as sent by chatbot to avoid pausing when webhook echoes it back
        _bot_mark_sent(owner_user_id, oa_id, to_user_id, text, ttl_minutes=5)
        # Try to record message id from API response if available
        try:
            msg_id = None
            if isinstance(result, dict):
                msg_id = result.get("message_id") or result.get("msg_id")
                if not msg_id and isinstance(result.get("data"), dict):
                    msg_id = result.get("data", {}).get("message_id") or result.get("data", {}).get("msg_id")
            if msg_id:
                _bot_mark_sent_id(owner_user_id, oa_id, to_user_id, str(msg_id), ttl_minutes=5)
        except Exception:
            pass
    except Exception:
        # best-effort
        pass


async def _get_user_api_key(db: AsyncSession, user_id) -> str | None:
    try:
        rec = await UserApiKeyRepository.get_by_user_id(db, user_id)
        return rec.api_key if rec and getattr(rec, "is_active", True) else None
    except Exception:
        return None

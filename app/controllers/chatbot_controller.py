from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
import logging
import os
import httpx

from app.database.database import get_db
from app.middlewares.api_key_middleware import get_user_for_chatbot
from app.middlewares.auth_middleware import get_current_active_superuser
from app.models.user import User
from app.dto.chatbot_dto import ChatRequest
from app.dto.response import ResponseModel
from app.services.chatbot_service import ChatbotService
from app.repositories.user_bot_control_repository import UserBotControlRepository

logger = logging.getLogger(__name__)
CHATBOT_API_BASE_URL = os.getenv("CHATBOT_API_BASE_URL", "http://localhost:8001")
router = APIRouter(tags=["Chatbot"])

@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User, list[str]] = Depends(get_user_for_chatbot),
    http_request: Request = None,
):
    try:
        current_user, scopes = auth_result
        platform = getattr(request, 'platform', None)
        try:
            x_key = http_request.headers.get("x-api-key") if http_request else None
        except Exception:
            x_key = None
        if x_key:
            if not platform:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Thiếu tham số platform")
            enabled = await UserBotControlRepository.is_enabled(db, current_user.id, platform)
            if not enabled:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Nền tảng này đang tắt."
                )
        
        # Logic lấy API key của LLM provider (Gemini/OpenAI) vẫn như cũ
        api_key = request.api_key
        if not api_key:
            if request.llm_provider == "google_genai":
                api_key = current_user.gemini_api_key
            elif request.llm_provider == "openai":
                api_key = current_user.openai_api_key
        
        # Decrypt the API key if it exists
        if api_key:
            try:
                from app.utils.crypto import token_encryption
                api_key = token_encryption.decrypt(api_key)
            except Exception as e:
                logger.error(f"Error decrypting API key: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Lỗi khi giải mã API key."
                )
        
        # Validate that api_key exists
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bạn chưa thêm API key bên trang cấu hình."
            )
        
        # Prefer provided thread_id (e.g., from Zalo thread) if available; otherwise use user_id
        thread_id = str(request.thread_id) if getattr(request, 'thread_id', None) else str(current_user.id)
        customer_id = str(current_user.id)
        
        logger.info(f"Chatbot request - User: {current_user.id}, Scopes: {scopes}")
        
        if request.stream:
            return StreamingResponse(
                ChatbotService.stream_chat_with_bot(
                    thread_id=thread_id,
                    query=request.query,
                    customer_id=customer_id,
                    llm_provider=request.llm_provider,
                    api_key=api_key,
                    scopes=scopes,
                    image_url=getattr(request, 'image_url', None),
                    image_base64=getattr(request, 'image_base64', None),
                ),
                media_type="text/event-stream"
            )
        else:
            response = await ChatbotService.chat_with_bot(
                thread_id=thread_id,
                query=request.query,
                customer_id=customer_id,
                llm_provider=request.llm_provider,
                api_key=api_key,
                scopes=scopes,
                image_url=getattr(request, 'image_url', None),
                image_base64=getattr(request, 'image_base64', None),
                history=getattr(request, 'history', None),
            )
            return ResponseModel.success(data=response, message="Chatbot response")
    except HTTPException:
        # Re-raise HTTP exceptions without modification
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@router.get("/chat-history/{thread_id}")
async def get_chat_history_endpoint(
    thread_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User, list[str]] = Depends(get_user_for_chatbot)
):
    try:
        current_user, _ = auth_result
        customer_id = str(current_user.id)
        data = await ChatbotService.get_chat_history(customer_id=customer_id, thread_id=thread_id, limit=limit)
        return ResponseModel.success(data=data, message="Chat history")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.post("/clear-history-chat")
async def clear_history_chat(
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User, list[str]] = Depends(get_user_for_chatbot)
):
    try:
        current_user, scopes = auth_result
        await ChatbotService.clear_history_chat(current_user)
        return ResponseModel.success(message="Lịch sử chat đã được xóa")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

async def _call_chatbot_api(endpoint: str, method: str = "GET", data: dict | None = None):
    try:
        async with httpx.AsyncClient() as client:
            url = f"{CHATBOT_API_BASE_URL}{endpoint}"
            headers = {"Content-Type": "application/json"}
            if method == "GET":
                resp = await client.get(url)
            elif method == "PUT":
                resp = await client.put(url, json=data, headers=headers)
            elif method == "POST":
                resp = await client.post(url, json=data, headers=headers)
            elif method == "DELETE":
                resp = await client.delete(url)
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Method not supported")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.get("/admin/instructions")
async def admin_get_instructions(current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api("/instructions", "GET")

@router.get("/admin/instructions/{key}")
async def admin_get_instruction_by_key(key: str, current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api(f"/instructions/{key}", "GET")

@router.post("/admin/instructions")
async def admin_create_instruction(item: dict, current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api("/instructions", "POST", data=item)

@router.put("/admin/instructions")
async def admin_update_instructions(update_data: dict, current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api("/instructions", "PUT", data=update_data)

@router.put("/admin/instructions/{key}")
async def admin_upsert_instruction(key: str, item: dict, current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api(f"/instructions/{key}", "PUT", data=item)

@router.delete("/admin/instructions/{key}")
async def admin_delete_instruction(key: str, current_user: User = Depends(get_current_active_superuser)):
    return await _call_chatbot_api(f"/instructions/{key}", "DELETE")
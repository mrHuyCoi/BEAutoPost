from fastapi import APIRouter, Depends, HTTPException, status, Body, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List, Dict
import traceback
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database.database import get_db
from app.middlewares.subscription_middleware import check_active_subscription
from app.services.scheduled_post_service import ScheduledPostService
from app.schemas.schedule_schema import PlatformPostResponse
from app.utils.crypto import token_encryption
from app.repositories.platform_post_repository import PlatformPostRepository

router = APIRouter()

@router.post("/generate-review")
async def generate_review_content(
    prompt: str = Body(..., embed=True),
    platform_type: List[str] = Body(..., embed=True),
    hashtags: Optional[List[str]] = Body(None, embed=True),
    brand_name: Optional[str] = Body(None, embed=True),
    call_to_action: Optional[str] = Body(None, embed=True),
    posting_purpose: Optional[str] = Body(None, embed=True),
    ai_platform: str = Body("gemini", embed=True),
    current_user=Depends(check_active_subscription(required_max_videos_per_day=1)),
):
    """
    Endpoint để sinh trước nội dung review cho bài đăng.
    - Logic được xử lý trong ScheduledPostService.
    """
    try:
        result = await ScheduledPostService.generate_preview_content(
            prompt=prompt,
            platform_type=platform_type,
            hashtags=hashtags,
            brand_name=brand_name,
            posting_purpose=posting_purpose,
            ai_platform=ai_platform,
            call_to_action=call_to_action,
            current_user_id=current_user.id,
            current_user_custom_prompt=current_user.custom_system_prompt,
            current_user_openai_api_key=current_user.openai_api_key,
            current_user_gemini_api_key=current_user.gemini_api_key
        )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi sinh nội dung: {str(e)}")


@router.post("/schedule-post")
async def schedule_post(
    prompt: str = Form(...),
    preview_content: str = Form(...),
    scheduled_at: str = Form(...),
    platform_specific_data: str = Form(...),
    media_files: Optional[List[UploadFile]] = File(None),
    brand_name: Optional[str] = Form(None),
    posting_purpose: Optional[str] = Form(None),
    publish_immediately: bool = Form(False),
    current_user=Depends(check_active_subscription()),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint để lên lịch đăng bài mới hoặc đăng bài ngay lập tức.
    - Nếu publish_immediately=True: Bài viết sẽ được đăng ngay lập tức.
    - Nếu publish_immediately=False: Bài viết sẽ được lên lịch đăng vào thời điểm scheduled_at.
    - Logic nghiệp vụ được chuyển sang ScheduledPostService.
    - Controller chỉ chịu trách nhiệm nhận request và gọi service.
    """
    try:
        effective_scheduled_at = scheduled_at
        if publish_immediately:
            # Nếu đăng ngay, ghi đè thời gian hẹn lịch bằng thời gian hiện tại
            now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
            effective_scheduled_at = now.isoformat()

        # 1. Luôn tạo bản ghi bài đăng trong DB trước
        result = await ScheduledPostService.schedule_new_post(
            db=db,
            current_user_id=current_user.id,
            prompt=prompt,
            preview_content=preview_content,
            scheduled_at=effective_scheduled_at,
            platform_specific_data=platform_specific_data,
            media_files=media_files,
            brand_name=brand_name,
            posting_purpose=posting_purpose,
            publish_immediately=publish_immediately
        )

        # 2. Nếu yêu cầu đăng ngay, gọi service để đăng
        if publish_immediately:
            post_ids = result.get("post_ids", [])
            for post_id in post_ids:
                await ScheduledPostService.publish_post_immediately(
                    db=db,
                    post_id=post_id,
                    current_user_id=current_user.id
                )
            return {"message": "Đã đăng bài thành công", "post_ids": post_ids}

        # 3. Nếu chỉ lên lịch, trả về kết quả từ schedule_new_post
        return result
    except HTTPException as e:
        # Ném lại các lỗi HTTP đã được xử lý từ service
        raise e
    except Exception as e:
        # Bắt các lỗi không mong muốn khác
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lỗi máy chủ nội bộ khi xử lý bài đăng: {str(e)}")


@router.get("/platform-posts", response_model=List[PlatformPostResponse])
async def get_platform_posts_by_user(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(check_active_subscription()),
    skip: int = 0,
    limit: int = 100
):
    """
    Lấy danh sách platform_post theo user hiện tại.
    """
    try:
        return await ScheduledPostService.get_user_platform_posts(
            db=db,
            current_user_id=current_user.id,
            skip=skip,
            limit=limit
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi lấy danh sách platform_post: {e}")

@router.get("/platform-posts/published", response_model=List[PlatformPostResponse])
async def get_published_platform_posts_by_user(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(check_active_subscription()),
    skip: int = 0,
    limit: int = 100
):
    """
    Lấy danh sách platform_post đã đăng (status='published') theo user hiện tại.
    """
    try:
        return await ScheduledPostService.get_user_platform_posts(
            db=db,
            current_user_id=current_user.id,
            skip=skip,
            limit=limit,
            status_filter="published"
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi lấy danh sách platform_post đã đăng: {e}")

@router.get("/platform-posts/unpublished", response_model=List[PlatformPostResponse])
async def get_unpublished_platform_posts_by_user(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(check_active_subscription()),
    skip: int = 0,
    limit: int = 100
):
    """
    Lấy danh sách platform_post chưa đăng (status khác 'published') theo user hiện tại.
    """
    try:
        return await ScheduledPostService.get_user_platform_posts(
            db=db,
            current_user_id=current_user.id,
            skip=skip,
            limit=limit,
            status_filter="unpublished" # Hoặc một giá trị khác để lọc các trạng thái chưa đăng
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi lấy danh sách platform_post chưa đăng: {e}")


@router.put("/platform-posts/{post_id}", response_model=dict)
async def update_scheduled_post(
    post_id: uuid.UUID,
    preview_content: Optional[str] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    current_user=Depends(check_active_subscription()),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint cho phép cập nhật trường preview_content và scheduled_at của bài đăng đã lên lịch.
    """
    try:
        result = await ScheduledPostService.update_scheduled_post(
            db=db,
            post_id=post_id,
            current_user_id=current_user.id,
            preview_content=preview_content,
            scheduled_at=scheduled_at
        )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lỗi máy chủ nội bộ khi cập nhật bài đăng: {str(e)}")


@router.patch("/platform-posts/{post_id}/retry", response_model=dict)
async def retry_failed_post(
    post_id: uuid.UUID,
    current_user=Depends(check_active_subscription()),
    db: AsyncSession = Depends(get_db)
):
    """
    Chuyển đổi trạng thái bài đăng từ 'failed' thành 'ready' để có thể thử đăng lại.
    """
    try:
        # Lấy thông tin bài đăng
        platform_post = await PlatformPostRepository.get_platform_post_by_id(db, post_id, current_user.id)
        if not platform_post:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy bài đăng")
        
        # Kiểm tra trạng thái hiện tại
        if platform_post.status != "failed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chỉ có thể thử lại các bài đăng có trạng thái 'failed'"
            )
        
        # Cập nhật trạng thái thành 'ready'
        update_data = {
            "status": "ready",
            "updated_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(tzinfo=None)
        }
        
        success = await PlatformPostRepository.update_platform_post(db, post_id, current_user.id, update_data)
        if not success:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Không thể cập nhật trạng thái bài đăng")
        
        return {"message": "Đã chuyển trạng thái bài đăng thành 'ready' để thử lại", "post_id": post_id}
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lỗi máy chủ nội bộ: {str(e)}")

@router.post("/platform-posts/{post_id}/publish", response_model=dict)
async def publish_post(
    post_id: uuid.UUID,
    current_user=Depends(check_active_subscription()),
    db: AsyncSession = Depends(get_db)
):
    """
    Đăng lại bài đăng có trạng thái 'ready'.
    """
    try:
        # Lấy thông tin bài đăng
        platform_post = await PlatformPostRepository.get_platform_post_by_id(db, post_id, current_user.id)
        if not platform_post:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy bài đăng")
        
        # Kiểm tra trạng thái hiện tại
        if platform_post.status != "ready":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chỉ có thể đăng các bài đăng có trạng thái 'ready'"
            )
        
        # Gọi service để đăng bài
        await ScheduledPostService.publish_post_immediately(db, post_id, current_user.id)
        
        return {"message": "Đã gửi yêu cầu đăng bài", "post_id": post_id}
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lỗi máy chủ nội bộ: {str(e)}")

@router.delete("/platform-posts/{post_id}", response_model=dict)
async def delete_scheduled_post(
    post_id: uuid.UUID,
    current_user=Depends(check_active_subscription()),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint để xóa bài đăng đã lên lịch.
    - Chỉ có thể xóa bài đăng chưa được xuất bản.
    """
    try:
        result = await ScheduledPostService.delete_scheduled_post(
            db=db,
            post_id=post_id,
            current_user_id=current_user.id
        )
        return result
    except HTTPException as e:
        # Ném lại các lỗi HTTP đã được xử lý từ service
        raise e
    except Exception as e:
        # Bắt các lỗi không mong muốn khác
        traceback.print_exc()
        # get_db dependency sẽ tự động rollback
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lỗi máy chủ nội bộ khi xóa bài đăng: {str(e)}")

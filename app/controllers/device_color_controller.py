from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from typing import List, Optional

from app.database.database import get_db
from app.models.user import User
from app.middlewares.auth_middleware import get_current_user
from app.services.device_color_service import DeviceColorService
from app.dto.device_color_dto import DeviceColorCreate, DeviceColorRead, DeviceColorWithColorRead
from app.dto.color_dto import ColorRead
from app.dto.response import ResponseModel
from app.exceptions.not_found_exception import NotFoundException


router = APIRouter(
    prefix="/device-colors",
    tags=["Device Colors"]
)


@router.post("", response_model=ResponseModel[DeviceColorRead])
async def create_device_color(
    data: DeviceColorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Tạo một liên kết giữa thiết bị và màu sắc mới.
    """
    try:
        # Truyền current_user vào service để phân quyền admin
        device_color = await DeviceColorService.create_device_color(db, data, current_user)
        return ResponseModel(
            data=device_color,
            message="Tạo liên kết giữa thiết bị và màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.get("", response_model=ResponseModel[List[DeviceColorWithColorRead]])
async def get_all_device_colors(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lấy danh sách tất cả liên kết giữa thiết bị và màu sắc với phân trang và tìm kiếm.
    """
    try:
        # Chỉ lấy liên kết của user hiện tại hoặc liên kết mặc định
        device_colors = await DeviceColorService.get_all_device_colors(db, skip, limit, search, current_user.id)
        total = await DeviceColorService.count_device_colors(db, search, current_user.id)
        
        return ResponseModel.success(
            data=device_colors,
            message="Lấy danh sách liên kết giữa thiết bị và màu sắc thành công",
            total=total,
            totalPages=(total + limit - 1) // limit
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.get("/{device_color_id}", response_model=ResponseModel[DeviceColorRead])
async def get_device_color(
    device_color_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lấy thông tin liên kết giữa thiết bị và màu sắc theo ID.
    """
    try:
        # Chỉ lấy liên kết của user hiện tại hoặc liên kết mặc định
        device_color = await DeviceColorService.get_device_color(db, device_color_id, current_user.id)
        return ResponseModel(
            data=device_color,
            message="Lấy thông tin liên kết giữa thiết bị và màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.get("/device/{device_info_id}", response_model=ResponseModel[List[DeviceColorRead]])
async def get_device_colors_by_device_info_id(
    device_info_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lấy tất cả các liên kết giữa thiết bị và màu sắc theo ID của thiết bị.
    """
    try:
        # Chỉ lấy liên kết của user hiện tại hoặc liên kết mặc định
        device_colors = await DeviceColorService.get_device_colors_by_device_info_id(db, device_info_id, current_user.id)
        return ResponseModel(
            data=device_colors,
            message="Lấy danh sách liên kết giữa thiết bị và màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.get("/device/{device_info_id}/with-color", response_model=ResponseModel[List[DeviceColorWithColorRead]])
async def get_device_colors_with_color_by_device_info_id(
    device_info_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lấy tất cả các liên kết giữa thiết bị và màu sắc theo ID của thiết bị kèm thông tin màu sắc.
    """
    try:
        # Chỉ lấy liên kết của user hiện tại hoặc liên kết mặc định
        device_colors = await DeviceColorService.get_device_colors_with_color_by_device_info_id(db, device_info_id, current_user.id)
        return ResponseModel(
            data=device_colors,
            message="Lấy danh sách liên kết giữa thiết bị và màu sắc kèm thông tin màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.get("/device/{device_info_id}/colors", response_model=ResponseModel[List[ColorRead]])
async def get_colors_by_device_info_id(
    device_info_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lấy tất cả các màu sắc của một thiết bị theo ID của thiết bị.
    """
    try:
        # Chỉ lấy màu sắc từ liên kết của user hiện tại hoặc liên kết mặc định
        colors = await DeviceColorService.get_colors_by_device_info_id(db, device_info_id, current_user.id)
        return ResponseModel(
            data=colors,
            message="Lấy danh sách màu sắc của thiết bị thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.delete("/{device_color_id}", response_model=ResponseModel[bool])
async def delete_device_color(
    device_color_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Xóa một liên kết giữa thiết bị và màu sắc.
    """
    try:
        # Chỉ cho phép xóa liên kết của chính mình
        result = await DeviceColorService.delete_device_color(db, device_color_id, current_user)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bạn không có quyền xóa liên kết này."
            )
        return ResponseModel.success(
            data=result,
            message="Xóa liên kết giữa thiết bị và màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )


@router.delete("/device/{device_info_id}", response_model=ResponseModel[bool])
async def delete_device_colors_by_device_info_id(
    device_info_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Xóa tất cả các liên kết giữa thiết bị và màu sắc theo ID của thiết bị.
    """
    try:
        # Chỉ cho phép xóa liên kết của chính mình
        result = await DeviceColorService.delete_device_colors_by_device_info_id(db, device_info_id, current_user.id)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Xóa liên kết thiết bị-màu sắc không thành công"
            )
        return ResponseModel(
            data=result,
            message="Xóa tất cả liên kết giữa thiết bị và màu sắc thành công"
        )
    except NotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Đã xảy ra lỗi: {str(e)}"
        )
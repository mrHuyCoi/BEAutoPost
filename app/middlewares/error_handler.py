from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.exceptions.base_exception import AppException

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """
    Middleware để xử lý lỗi từ application
    """
    
    async def dispatch(self, request: Request, call_next):
        try:
            # Tiếp tục xử lý request
            return await call_next(request)
        except AppException as e:
            # Xử lý các exception tùy chỉnh từ ứng dụng
            return JSONResponse(
                status_code=e.status_code,
                content={"message": e.message, "error_code": e.error_code}
            )
        except Exception as e:
            # Xử lý các exception không xác định
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "message": "Internal server error",
                    "error_code": "INTERNAL_SERVER_ERROR",
                    "detail": str(e) if request.app.debug else None
                }
            )

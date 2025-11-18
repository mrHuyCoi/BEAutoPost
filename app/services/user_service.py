from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
import uuid
from app.models.subscription import Subscription
from app.dto.user_dto import UserCreate, UserUpdate
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.exceptions.api_exceptions import BadRequestException, NotFoundException
from app.utils.security import verify_password, hash_password
from app.utils.crypto import token_encryption
from app.repositories.verification_code_repository import VerificationCodeRepository
from app.dto.verification_code_dto import VerificationCodeCreate
from app.services.email_service import EmailService
import random
import string
from datetime import datetime, timedelta, timezone

class UserService:
    """
    Service xử lý các thao tác liên quan đến người dùng.
    """
    
    @staticmethod
    async def send_registration_code(db: AsyncSession, email: str):
        """
        Gửi mã xác thực đăng ký đến email của người dùng.
        """
        try:
            # Kiểm tra email đã tồn tại chưa (case-insensitive)
            existing_user = await UserRepository.get_by_email(db, email.lower())
            if existing_user:
                raise BadRequestException("Email đã được đăng ký.")

            code = ''.join(random.choices(string.digits, k=6))
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

            code_data = VerificationCodeCreate(email=email.lower(), code=code, expires_at=expires_at)
            await VerificationCodeRepository.create(db, code_data)

            # Try to send email, but don't fail if email service is not configured
            try:
                await EmailService.send_verification_code(email=email, code=code)
            except Exception as email_error:
                print(f"Warning: Could not send email: {email_error}")
                # Continue without failing the registration process
                # In production, you might want to log this to a monitoring service
                
        except Exception as e:
            print(f"Error in send_registration_code: {str(e)}")
            raise e

    @staticmethod
    async def register_user(db: AsyncSession, data: UserCreate, verification_code: str) -> User:
        # 1. Xác thực mã
        code_obj = await VerificationCodeRepository.get_by_email_and_code(db, data.email, verification_code)
        if not code_obj:
            raise BadRequestException("Mã xác thực không hợp lệ hoặc đã hết hạn.")

        # 2. Lấy thông tin gói đăng ký đã chọn
        subscription_plan = await db.get(Subscription, data.subscription_id)
        if not subscription_plan:
            raise BadRequestException("Gói đăng ký không hợp lệ")

        # 3. Tạo người dùng mới
        new_user = await UserRepository.create(db, data)

        # 4. Tạo bản ghi user_subscription
        from app.repositories.subscription_repository import SubscriptionRepository
        from app.dto.subscription_dto import SubscriptionCreate as UserSubscriptionCreate
        
        current_time = datetime.now(timezone.utc)
        end_date = current_time + timedelta(days=subscription_plan.duration_days)

        user_sub_data = UserSubscriptionCreate(
            user_id=new_user.id,
            subscription_id=data.subscription_id,
            start_date=current_time,
            end_date=end_date,
            is_active=False
        )
        await SubscriptionRepository.create(db, user_sub_data)

        # 5. Xóa mã xác thực đã sử dụng
        await VerificationCodeRepository.delete(db, code_obj)

        return new_user

    @staticmethod
    async def register_user_direct(db: AsyncSession, data: UserCreate) -> User:
        """
        Đăng ký người dùng trực tiếp không cần mã xác thực.
        """
        # 1. Kiểm tra email đã tồn tại chưa (case-insensitive)
        existing_user = await UserRepository.get_by_email(db, data.email.lower())
        if existing_user:
            raise BadRequestException("Email đã được đăng ký.")

        # 2. Lấy thông tin gói đăng ký đã chọn
        subscription_plan = await db.get(Subscription, data.subscription_id)
        if not subscription_plan:
            raise BadRequestException("Gói đăng ký không hợp lệ")

        # 3. Tạo người dùng mới
        new_user = await UserRepository.create(db, data)

        # 4. Tạo bản ghi user_subscription
        from app.repositories.subscription_repository import SubscriptionRepository
        from app.dto.subscription_dto import SubscriptionCreate as UserSubscriptionCreate
        
        current_time = datetime.now(timezone.utc)
        end_date = current_time + timedelta(days=subscription_plan.duration_days)

        user_sub_data = UserSubscriptionCreate(
            user_id=new_user.id,
            subscription_id=data.subscription_id,
            start_date=current_time,
            end_date=end_date,
            is_active=False
        )
        await SubscriptionRepository.create(db, user_sub_data)

        return new_user

    @staticmethod
    async def send_password_reset_code(db: AsyncSession, email: str):
        user = await UserRepository.get_by_email(db, email.lower())
        if not user:
            # To prevent email enumeration attacks, we don't reveal if the user exists.
            # We'll just log a warning and proceed as if an email was sent.
            print(f"Password reset requested for non-existent user: {email}")
            return

        code = ''.join(random.choices(string.digits, k=6))
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

        code_data = VerificationCodeCreate(email=email.lower(), code=code, expires_at=expires_at)
        await VerificationCodeRepository.create(db, code_data)

        try:
            await EmailService.send_password_reset_email(email=email, code=code)
        except Exception as email_error:
            print(f"Warning: Could not send password reset email: {email_error}")

    @staticmethod
    async def reset_password(db: AsyncSession, email: str, code: str, new_password: str):
        code_obj = await VerificationCodeRepository.get_by_email_and_code(db, email.lower(), code)
        if not code_obj:
            raise BadRequestException("Invalid or expired verification code.")

        user = await UserRepository.get_by_email(db, email.lower())
        if not user:
            raise NotFoundException("User not found.") # Should not happen if code is valid

        user.hashed_password = hash_password(new_password)
        await db.commit()

        await VerificationCodeRepository.delete(db, code_obj)
    
    @staticmethod
    async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("Không tìm thấy người dùng")
        return user
    
    @staticmethod
    async def update_user(db: AsyncSession, user_id: uuid.UUID, data: UserUpdate) -> User:
        db_user = await UserRepository.get_by_id(db, user_id)
        if not db_user:
            raise NotFoundException("Không tìm thấy người dùng")

        update_data = data.dict(exclude_unset=True)
        if 'full_name' in update_data:
            db_user.full_name = update_data['full_name']
        if 'is_active' in update_data:
            db_user.is_active = update_data['is_active']
        if 'custom_system_prompt' in update_data:
            db_user.custom_system_prompt = update_data['custom_system_prompt']
        if 'role' in update_data:
            db_user.role = update_data['role']

        await db.commit()
        await db.refresh(db_user)
        return db_user

    @staticmethod
    async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
        user = await UserRepository.get_by_email(db, email)
        if not user or not verify_password(password, user.hashed_password) or not user.is_active:
            return None
        return user

    @staticmethod
    async def update_system_prompt(db: AsyncSession, user_id: uuid.UUID, prompt: Optional[str]) -> User:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("Không tìm thấy người dùng")
        user.custom_system_prompt = prompt
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def update_api_keys(db: AsyncSession, user_id: uuid.UUID, gemini_key: Optional[str], openai_key: Optional[str]) -> dict:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("Không tìm thấy người dùng")
        if gemini_key is not None:
            user.gemini_api_key = token_encryption.encrypt(gemini_key)
        if openai_key is not None:
            user.openai_api_key = token_encryption.encrypt(openai_key)
        await db.commit()
        await db.refresh(user)
        return {
            "gemini_api_key": gemini_key,
            "openai_api_key": openai_key
        }

    @staticmethod
    async def get_all_users(db: AsyncSession, skip: int = 0, limit: int = 100) -> List[User]:
        return await UserRepository.get_all(db, skip, limit)

    @staticmethod
    async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> bool:
        return await UserRepository.delete(db, user_id)

    @staticmethod
    async def get_api_keys(db: AsyncSession, user_id: uuid.UUID) -> dict:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("Không tìm thấy người dùng")
        return {
            "gemini_api_key": token_encryption.decrypt(user.gemini_api_key) if user.gemini_api_key else None,
            "openai_api_key": token_encryption.decrypt(user.openai_api_key) if user.openai_api_key else None
        }

    @staticmethod
    async def delete_api_keys(db: AsyncSession, user_id: uuid.UUID, gemini_key: Optional[bool], openai_key: Optional[bool]) -> dict:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("Không tìm thấy người dùng")
        if gemini_key:
            user.gemini_api_key = None
        if openai_key:
            user.openai_api_key = None
        await db.commit()
        await db.refresh(user)
        return {
            "gemini_api_key": None if gemini_key else (token_encryption.decrypt(user.gemini_api_key) if user.gemini_api_key else None),
            "openai_api_key": None if openai_key else (token_encryption.decrypt(user.openai_api_key) if user.openai_api_key else None)
        }
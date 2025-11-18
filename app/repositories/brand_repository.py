from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, asc, desc, func, Integer, or_, and_
from sqlalchemy.orm import selectinload, joinedload
from typing import Optional, List
import uuid
import re
from datetime import date

from app.models.brand import Brand
from app.dto.brand_dto import BrandCreate, BrandUpdate
from app.models.service import Service


class BrandRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _format_warranty(warranty: str) -> str:
        """
        Định dạng trường bảo hành.
        """
        if warranty:
            if re.match(r'^\d+$', warranty.strip()):
                warranty = f"{warranty.strip()} tháng"
        return warranty

    @staticmethod
    async def get_last_service_code(db: AsyncSession, user_id: Optional[uuid.UUID] = None) -> Optional[str]:
        query = (
            select(Brand.service_code)
            .join(Service, Brand.service_id == Service.id)
            .where(Brand.trashed_at.is_(None))
            .order_by(Brand.created_at.desc())
            .limit(1)
        )
        if user_id:
            query = query.where(Service.user_id == user_id)
        result = await db.execute(query)
        return result.scalars().first()
    
    @staticmethod
    async def get_by_service_code(db: AsyncSession, service_code: str, user_id: Optional[uuid.UUID] = None) -> Optional[Brand]:
        query = (
            select(Brand)
            .join(Service, Brand.service_id == Service.id)
            .where(func.lower(Brand.service_code) == func.lower(service_code), Brand.trashed_at.is_(None))
        )
        if user_id:
            query = query.where(Service.user_id == user_id)
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def _generate_unique_service_code(db: AsyncSession, user_id: Optional[uuid.UUID] = None) -> str:
        """
        Generate a unique service code that doesn't already exist in the database.
        """
        query = (
            select(Brand.service_code)
            .select_from(Brand)
            .join(Service, Brand.service_id == Service.id)
            .where(Brand.service_code.like("DV%"), Brand.trashed_at.is_(None))
            .order_by(Brand.created_at.desc())
        )
        if user_id:
            query = query.where(Service.user_id == user_id)
        
        result = await db.execute(query)
        existing_codes = result.scalars().all()
        
        max_number = 0
        for code in existing_codes:
            if code and code.startswith("DV") and len(code) >= 3:
                try:
                    number_part = code[2:]
                    if number_part.isdigit():
                        max_number = max(max_number, int(number_part))
                except (ValueError, IndexError):
                    continue
        
        attempt = 0
        max_attempts = 1000 
        
        while attempt < max_attempts:
            new_number = max_number + 1 + attempt
            new_code = f"DV{new_number:06d}"
            
            existing = await BrandRepository.get_by_service_code(db, new_code, user_id)
            if not existing:
                return new_code
                
            attempt += 1
        
        import time
        timestamp_suffix = str(int(time.time()))[-6:]
        return f"DV{timestamp_suffix}"

    @staticmethod
    async def create(db: AsyncSession, brand: Brand, user_id: uuid.UUID) -> Brand:
        # Generate unique service code if not provided (scoped by user)
        if not brand.service_code:
            brand.service_code = await BrandRepository._generate_unique_service_code(db, user_id)
        else:
            existing_with_code = await BrandRepository.get_by_service_code(db, brand.service_code, user_id)
            if existing_with_code:
                brand.service_code = await BrandRepository._generate_unique_service_code(db, user_id)

        # Xử lý trường bảo hành
        if brand.warranty:
            brand.warranty = BrandRepository._format_warranty(brand.warranty)

        db.add(brand)
        await db.commit()
        
        # --- SỬA LỖI TẠI ĐÂY ---
        # Bỏ 'refresh' vì nó không đáng tin cậy
        # await db.refresh(brand) 
        
        # Thay vào đó, get() lại object bằng ID để đảm bảo
        # lấy được các trường 'created_at', 'updated_at'
        new_brand = await BrandRepository.get_by_id_with_details(db, brand.id)
        if not new_brand:
            # Điều này không bao giờ nên xảy ra
            raise Exception("Không thể tạo hoặc làm mới thương hiệu")
            
        return new_brand
        # --- KẾT THÚC SỬA ---

    @staticmethod
    async def get_by_id(db: AsyncSession, brand_id: uuid.UUID) -> Optional[Brand]:
        query = select(Brand).where(Brand.id == brand_id, Brand.trashed_at.is_(None))
        result = await db.execute(query)
        return result.scalars().first()
    
    @staticmethod
    async def get_by_id_with_details(db: AsyncSession, brand_id: uuid.UUID) -> Optional[Brand]:
        query = select(Brand).options(
            selectinload(Brand.service),
            selectinload(Brand.device_brand)
        ).where(Brand.id == brand_id, Brand.trashed_at.is_(None))
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def get_by_name(db: AsyncSession, name: str, service_id: uuid.UUID, user_id: Optional[uuid.UUID] = None) -> Optional[Brand]:
        query = (
            select(Brand)
            .join(Service, Brand.service_id == Service.id)
            .where(
                Brand.name == name,
                Brand.service_id == service_id,
                Brand.trashed_at.is_(None)
            )
        )
        if user_id:
            query = query.where(Service.user_id == user_id)
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def find_duplicate(
        db: AsyncSession,
        name: str,
        service_id: uuid.UUID,
        device_brand_id: Optional[uuid.UUID],
        device_type: Optional[str],
        color: Optional[str],
        price: Optional[str],
        wholesale_price: Optional[str],
        warranty: Optional[str],
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[Brand]:
        query = (
            select(Brand)
            .join(Service, Brand.service_id == Service.id)
            .where(
                Brand.name == name,
                Brand.service_id == service_id,
                Brand.device_brand_id == device_brand_id,
                Brand.device_type == device_type,
                Brand.color == color,
                Brand.price == price,
                Brand.wholesale_price == wholesale_price,
                Brand.warranty == warranty,
                Brand.trashed_at.is_(None)
            )
        )
        if user_id:
            query = query.where(Service.user_id == user_id)
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def update(db: AsyncSession, brand_id: uuid.UUID, data: BrandUpdate) -> Optional[Brand]:
        db_brand = await BrandRepository.get_by_id(db, brand_id)
        if not db_brand:
            return None
        update_data = data.model_dump(exclude_unset=True) # Sửa: .dict() -> .model_dump()
        for key, value in update_data.items():
            if key == 'warranty':
                value = BrandRepository._format_warranty(value)
            setattr(db_brand, key, value)
        await db.commit()
        await db.refresh(db_brand)
        return db_brand

    @staticmethod
    async def delete(db: AsyncSession, brand_id: uuid.UUID) -> bool:
        db_brand = await BrandRepository.get_by_id(db, brand_id)
        if not db_brand:
            return False
        await db.delete(db_brand)
        await db.commit()
        return True

    @staticmethod
    async def get_all(db: AsyncSession, skip: int = 0, limit: int = 1000, search: Optional[str] = None, service_id: Optional[uuid.UUID] = None, sort_by: Optional[str] = None, sort_order: Optional[str] = 'asc', user_id: Optional[uuid.UUID] = None) -> List[Brand]:
        query = select(Brand).options(
            selectinload(Brand.service),
            selectinload(Brand.device_brand)
        ).join(Service, Brand.service_id == Service.id).where(Brand.trashed_at.is_(None))

        if user_id:
            query = query.where(Service.user_id == user_id)
            
        if service_id:
            query = query.where(Brand.service_id == service_id)
        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    Brand.name.ilike(search_term),
                    Brand.service_code.ilike(search_term),
                    Brand.device_type.ilike(search_term)
                )
            )
        
        if sort_by:
            column = getattr(Brand, sort_by, None)
            if column:
                if sort_order == 'desc':
                    query = query.order_by(desc(column))
                else:
                    query = query.order_by(asc(column))
            else:
                query = query.order_by(Brand.created_at.desc())
        else:
            query = query.order_by(Brand.created_at.desc())
            
        query = query.offset(skip).limit(limit)
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def count_all(db: AsyncSession, search: Optional[str] = None, service_id: Optional[uuid.UUID] = None, user_id: Optional[uuid.UUID] = None) -> int:
        from sqlalchemy import func
        query = select(func.count(Brand.id)).join(Service, Brand.service_id == Service.id).where(Brand.trashed_at.is_(None))

        if user_id:
            query = query.where(Service.user_id == user_id)

        if service_id:
            query = query.where(Brand.service_id == service_id)
        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    Brand.name.ilike(search_term),
                    Brand.service_code.ilike(search_term),
                    Brand.device_type.ilike(search_term)
                )
            )
        result = await db.execute(query)
        return result.scalar_one()

    @staticmethod
    async def get_unique_brand_names_with_warranty(db: AsyncSession, service_id: uuid.UUID, user_id: Optional[uuid.UUID] = None) -> List[dict]:
        subquery_base = (
            select(
                Brand.name,
                Brand.warranty,
                func.row_number().over(
                    partition_by=Brand.name,
                    order_by=Brand.created_at.desc()
                ).label('row_num')
            )
            .select_from(Brand)
            .join(Service, Brand.service_id == Service.id)
            .where(Brand.service_id == service_id, Brand.trashed_at.is_(None))
        )
        if user_id:
            subquery_base = subquery_base.where(Service.user_id == user_id)
        subquery = subquery_base.subquery()

        query = select(subquery.c.name, subquery.c.warranty).where(subquery.c.row_num == 1)
        result = await db.execute(query)
        
        return [{"name": row[0], "warranty": row[1]} for row in result.fetchall()]

    @staticmethod
    async def get_deleted_today_by_user_id(db: AsyncSession, user_id: uuid.UUID) -> List[Brand]:
        today = date.today()
        query = select(Brand).options(
            joinedload(Brand.service),
            joinedload(Brand.device_brand)
        ).join(Service, Brand.service_id == Service.id).where(
            and_(
                Service.user_id == user_id,
                Brand.trashed_at.isnot(None),
                func.date(Brand.trashed_at) == today
            )
        )
        result = await db.execute(query)
        return result.scalars().all()
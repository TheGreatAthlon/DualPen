from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.auth import require_admin
from server.app.db import get_db
from server.app.models import User
from server.app.schemas import CreateUserRequest, UpdateUserRequest, UserOut
from server.app.security import hash_password
from server.app.user_service import create_user, UsernameTakenError

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(payload: CreateUserRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await create_user(
            db,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.initial_password,
        )
    except UsernameTakenError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, payload: UpdateUserRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.new_password is not None:
        user.password_hash = hash_password(payload.new_password)
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.is_active is not None:
        user.is_active = payload.is_active

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", response_model=UserOut)
async def deactivate_user(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.is_active = False
    await db.commit()
    await db.refresh(user)
    return user

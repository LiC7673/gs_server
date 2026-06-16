import asyncio
from app.core.database import init_db, engine
from app.core.security import hash_password
from app.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def main():
    await init_db()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == "admin"))
        if not result.scalar_one_or_none():
            admin = User(
                username="admin",
                email="admin@example.com",
                hashed_password=hash_password("admin123"),
                nickname="Administrator",
                is_admin=True,
            )
            session.add(admin)
            await session.commit()
            print("Admin user created: admin / admin123")
        else:
            print("Admin user already exists")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

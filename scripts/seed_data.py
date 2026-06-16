import asyncio
from app.core.database import init_db, engine
from app.core.security import hash_password
from app.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def main():
    await init_db()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        users_data = [
            {"username": "demo", "email": "demo@example.com", "password": "demo123"},
            {"username": "alice", "email": "alice@example.com", "password": "alice123"},
            {"username": "bob", "email": "bob@example.com", "password": "bob123"},
        ]
        for data in users_data:
            from sqlalchemy import select
            result = await session.execute(select(User).where(User.username == data["username"]))
            if not result.scalar_one_or_none():
                user = User(
                    username=data["username"],
                    email=data["email"],
                    hashed_password=hash_password(data["password"]),
                    nickname=data["username"],
                )
                session.add(user)
                print(f"User created: {data['username']} / {data['password']}")
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

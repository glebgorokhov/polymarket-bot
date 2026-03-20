"""
Repository for Settings DB operations.
Settings are stored as key/value strings and parsed on retrieval.
"""

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Setting


class SettingsRepo:
    """Data access layer for the settings table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return the string value for a key, or default if not found."""
        setting = await self._session.get(Setting, key)
        return setting.value if setting else default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        """Return a float setting value."""
        value = await self.get(key)
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    async def get_int(self, key: str, default: int = 0) -> int:
        """Return an integer setting value."""
        value = await self.get(key)
        try:
            return int(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    async def set(self, key: str, value: str) -> Setting:
        """Insert or update a setting by key."""
        setting = await self._session.get(Setting, key)
        if setting is None:
            setting = Setting(key=key, value=str(value))
            self._session.add(setting)
        else:
            setting.value = str(value)
            setting.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return setting

    async def get_all(self) -> Sequence[Setting]:
        """Return all settings rows."""
        result = await self._session.execute(select(Setting).order_by(Setting.key))
        return result.scalars().all()

    async def exists(self, key: str) -> bool:
        """Check if a setting key exists."""
        setting = await self._session.get(Setting, key)
        return setting is not None

    async def count(self) -> int:
        """Return the total number of settings rows."""
        result = await self._session.execute(select(Setting))
        return len(result.scalars().all())

    async def as_dict(self) -> dict[str, str]:
        """Return all settings as a plain dict."""
        rows = await self.get_all()
        return {s.key: s.value for s in rows}

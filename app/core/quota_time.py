from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings


def gpu_quota_zone() -> ZoneInfo:
    return ZoneInfo(settings.gpu_quota_timezone)


def gpu_usage_date(now: Optional[datetime] = None) -> date:
    zone = gpu_quota_zone()
    if now is None:
        return datetime.now(zone).date()
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    return now.astimezone(zone).date()


def gpu_quota_resets_at(now: Optional[datetime] = None) -> str:
    zone = gpu_quota_zone()
    if now is None:
        local_now = datetime.now(zone)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=zone)
    else:
        local_now = now.astimezone(zone)
    next_midnight = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=zone)
    return next_midnight.isoformat()

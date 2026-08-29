from datetime import datetime

from minerwatch.models import Miner


def is_working_time(miner: Miner, now: datetime) -> bool:
    schedule = miner.schedule
    if schedule is None:
        return False

    local_now = now.astimezone(schedule.timezone)
    t = local_now.hour * 60 + local_now.minute
    weekday = local_now.weekday()

    for window in schedule.windows:
        for r in window.ranges:
            if r.crosses_midnight:
                if weekday in window.days and t >= r.start:
                    return True
                if ((weekday - 1) % 7) in window.days and t < r.end:
                    return True
            else:
                if weekday in window.days and r.start <= t < r.end:
                    return True
    return False

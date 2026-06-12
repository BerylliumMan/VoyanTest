from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8), 'Asia/Shanghai')

def now() -> datetime:
    return datetime.now(CST)

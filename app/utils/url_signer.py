import hmac
import hashlib
import time
from urllib.parse import urlencode
from app.core.config import settings


def sign_url(url: str, expires_in: int = 3600) -> str:
    expiry = int(time.time()) + expires_in
    message = f"{url}|{expiry}"
    signature = hmac.new(
        settings.secret_key.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    params = urlencode({"expires": expiry, "signature": signature})
    return f"{url}?{params}"


def verify_signed_url(url: str, signature: str, expiry: int) -> bool:
    if int(time.time()) > expiry:
        return False
    message = f"{url.split('?')[0]}|{expiry}"
    expected = hmac.new(
        settings.secret_key.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    return hmac.compare_digest(expected, signature)

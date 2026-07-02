import base64
import hashlib
import hmac


def verify_webhook(webhook_data: bytes, hmac_header: str, client_secret: str) -> bool:
    """Authenticates a webhook payload using the store's API secret."""
    if not hmac_header or not client_secret:
        return False
    digest = hmac.new(client_secret.encode("utf-8"), webhook_data, digestmod=hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest)
    return hmac.compare_digest(computed_hmac, hmac_header.encode("utf-8"))

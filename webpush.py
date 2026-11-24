"""إرسال Web Push (RFC 8030 + تشفير aes128gcm حسب RFC 8291 + VAPID حسب RFC 8292).

وحدة مستقلة تعتمد على `cryptography` و`PyJWT` و`requests` فقط (كلها موجودة في requirements.txt).
لا تعرف شيئاً عن قاعدة البيانات أو FastAPI: تأخذ اشتراكاً (endpoint, p256dh, auth) وحمولة وتُرسلها.
"""
import base64, json, os, secrets, struct
from urllib.parse import urlparse

import jwt
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# خدمات الدفع المعروفة فقط. الاشتراك يأتي من العميل، فلا نسمح للسيرفر بالاتصال بأي عنوان (SSRF).
# يمكن إضافة مضيفين بمتغير البيئة PUSH_ALLOWED_HOSTS (مفصولين بفاصلة؛ البداية بنقطة = أي نطاق فرعي).
DEFAULT_HOSTS = (
    "fcm.googleapis.com", "android.googleapis.com",          # Chrome / Edge / Brave / Opera على أندرويد والكمبيوتر
    ".push.services.mozilla.com",                              # Firefox
    ".push.apple.com",                                         # Safari / iPhone (PWA مثبّت)
    ".notify.windows.com",                                     # Edge القديم / Windows
)


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def allowed_hosts():
    extra = tuple(h.strip().lower() for h in os.getenv("PUSH_ALLOWED_HOSTS", "").split(",") if h.strip())
    return DEFAULT_HOSTS + extra


def endpoint_ok(endpoint: str) -> bool:
    try:
        u = urlparse(endpoint)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.port not in (None, 443) or u.username or u.password or len(endpoint) > 700:
        return False
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in allowed_hosts())


def valid_keys(p256dh: str, auth: str) -> bool:
    """p256dh = نقطة P-256 غير مضغوطة (65 بايت)، auth = 16 بايت."""
    try:
        k, a = b64d(p256dh), b64d(auth)
        if len(k) != 65 or k[0] != 4 or len(a) != 16:
            return False
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), k)
        return True
    except Exception:
        return False


# ---------------- التشفير (RFC 8291) ----------------
def _hkdf(length: int, salt: bytes, info: bytes, ikm: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _pub_bytes(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes = None, as_private=None, rs: int = 4096) -> bytes:
    """يرجع جسم الطلب كاملاً (header + ciphertext) بصيغة aes128gcm. المعاملان salt/as_private للاختبار فقط."""
    ua_pub_b, auth_secret = b64d(p256dh), b64d(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_b)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_pub_b = _pub_bytes(as_private.public_key())
    salt = salt or secrets.token_bytes(16)
    ecdh = as_private.exchange(ec.ECDH(), ua_pub)
    ikm = _hkdf(32, auth_secret, b"WebPush: info\x00" + ua_pub_b + as_pub_b, ecdh)
    cek = _hkdf(16, salt, b"Content-Encoding: aes128gcm\x00", ikm)
    nonce = _hkdf(12, salt, b"Content-Encoding: nonce\x00", ikm)
    if len(payload) + 17 > rs:                       # سجل واحد فقط (الحمولة الصغيرة كافية لإشعارنا)
        raise ValueError("payload too large")
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # 0x02 = آخر سجل
    return salt + struct.pack(">IB", rs, len(as_pub_b)) + as_pub_b + ct


def decrypt(body: bytes, ua_private, auth: str) -> bytes:
    """فك التشفير من جهة المتصفح (للاختبار فقط)."""
    salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
    as_pub_b, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub_b = _pub_bytes(ua_private.public_key())
    ecdh = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub_b))
    ikm = _hkdf(32, b64d(auth), b"WebPush: info\x00" + ua_pub_b + as_pub_b, ecdh)
    cek = _hkdf(16, salt, b"Content-Encoding: aes128gcm\x00", ikm)
    nonce = _hkdf(12, salt, b"Content-Encoding: nonce\x00", ikm)
    pt = AESGCM(cek).decrypt(nonce, ct, None)
    return pt.rstrip(b"\x00")[:-1] if pt.endswith(b"\x02") else pt[:pt.rindex(b"\x02")]


# ---------------- VAPID (RFC 8292) ----------------
class Vapid:
    def __init__(self, private_b64u: str, subject: str):
        self.key = ec.derive_private_key(int.from_bytes(b64d(private_b64u), "big"), ec.SECP256R1())
        self.public = b64u(_pub_bytes(self.key.public_key()))      # يُعطى للمتصفح في subscribe()
        self.subject = subject

    def headers(self, endpoint: str, ttl_s: int = 12 * 3600) -> dict:
        import time
        u = urlparse(endpoint)
        tok = jwt.encode({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_s, "sub": self.subject}, self.key, algorithm="ES256")
        if isinstance(tok, bytes):
            tok = tok.decode()
        return {"Authorization": f"vapid t={tok}, k={self.public}"}


def load_vapid():
    """يرجع Vapid لو المفاتيح مضبوطة، وإلا None (الإشعارات تتعطل بهدوء ويبقى التثبيت يعمل)."""
    priv = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    if not priv:
        return None
    try:
        sub = os.getenv("VAPID_SUBJECT", "").strip() or "mailto:admin@dawrak.example"
        return Vapid(priv, sub)
    except Exception as e:
        print("VAPID key invalid:", e)
        return None


def generate_keys():
    k = ec.generate_private_key(ec.SECP256R1())
    raw = k.private_numbers().private_value.to_bytes(32, "big")
    return b64u(raw), b64u(_pub_bytes(k.public_key()))


# ---------------- الإرسال ----------------
def send(vapid: Vapid, endpoint: str, p256dh: str, auth: str, payload: dict, ttl: int = 3600, urgency: str = "high", timeout: float = 8.0):
    """إرسال متزامن (يُستدعى داخل thread). يرجع (status_code, text). 404/410 = الاشتراك انتهى ويجب حذفه."""
    if not endpoint_ok(endpoint):
        return 400, "endpoint not allowed"
    body = encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), p256dh, auth)
    h = {**vapid.headers(endpoint), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
         "TTL": str(ttl), "Urgency": urgency}
    r = requests.post(endpoint, data=body, headers=h, timeout=timeout, allow_redirects=False)
    return r.status_code, r.text[:200]

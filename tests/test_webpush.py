"""اختبار التشفير على قيم RFC 8291 (الملحق A) + اختبار ذهاب وعودة + VAPID + قائمة المضيفين المسموحة."""
import base64, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import webpush as w

# --- RFC 8291 Appendix A ---
AS_PRIV = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
AS_PUB  = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
UA_PRIV = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUB  = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH    = "BTBZMqHH6r4Tts7J_aSIgg"
SALT    = "DGv6ra1nlYgDCS1FRnbzlw"
EXP = dict(ecdh="kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs", ikm="S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg",
           cek="oIhVW04MRdy2XN9CiKLxTg", nonce="4h_95klXJ5E_qnoN")

def priv(b): return ec.derive_private_key(int.from_bytes(w.b64d(b), "big"), ec.SECP256R1())

def test_rfc_vector():
    asp, uap = priv(AS_PRIV), priv(UA_PRIV)
    assert w.b64u(w._pub_bytes(asp.public_key())) == AS_PUB and w.b64u(w._pub_bytes(uap.public_key())) == UA_PUB
    ecdh = asp.exchange(ec.ECDH(), uap.public_key())
    assert w.b64u(ecdh) == EXP["ecdh"]
    ikm = w._hkdf(32, w.b64d(AUTH), b"WebPush: info\x00" + w.b64d(UA_PUB) + w.b64d(AS_PUB), ecdh)
    assert w.b64u(ikm) == EXP["ikm"]
    assert w.b64u(w._hkdf(16, w.b64d(SALT), b"Content-Encoding: aes128gcm\x00", ikm)) == EXP["cek"]
    assert w.b64u(w._hkdf(12, w.b64d(SALT), b"Content-Encoding: nonce\x00", ikm)) == EXP["nonce"]
    body = w.encrypt(b"When I grow up, I want to be a watermelon", UA_PUB, AUTH, salt=w.b64d(SALT), as_private=asp)
    # الرأس: salt(16) + rs=4096 + idlen=65 + مفتاح الخادم
    assert body[:16] == w.b64d(SALT) and body[16:20] == b"\x00\x00\x10\x00" and body[20] == 65 and body[21:86] == w.b64d(AS_PUB)
    assert w.b64u(body) == "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"  # RFC 8291 §5
    assert w.decrypt(body, uap, AUTH) == b"When I grow up, I want to be a watermelon"

def test_roundtrip_arabic_random_keys():
    ua = ec.generate_private_key(ec.SECP256R1()); auth = os.urandom(16)
    p256 = w.b64u(w._pub_bytes(ua.public_key()))
    msg = json.dumps({"title": "جاء دورك الآن", "body": "توجّه إلى عيادة د. أحمد", "url": "/track"}, ensure_ascii=False).encode()
    body = w.encrypt(msg, p256, w.b64u(auth))
    assert w.decrypt(body, ua, w.b64u(auth)) == msg
    assert len(body) < 4096
    assert w.valid_keys(p256, w.b64u(auth)) and not w.valid_keys(p256, "AAAA") and not w.valid_keys("AAAA", w.b64u(auth))

def test_vapid_jwt_verifies():
    p, pub = w.generate_keys()
    v = w.Vapid(p, "mailto:a@b.co"); assert v.public == pub
    h = v.headers("https://fcm.googleapis.com/fcm/send/abc")["Authorization"]
    assert h.startswith("vapid t=") and h.endswith(", k=" + pub)
    tok = h[len("vapid t="):h.index(", k=")]
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), w.b64d(pub))
    claims = jwt.decode(tok, pk, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:a@b.co" and 0 < claims["exp"] - __import__("time").time() <= 86400
    assert len(w.b64d(p)) == 32 and len(w.b64d(pub)) == 65

def test_endpoint_allowlist():
    ok = ["https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
          "https://web.push.apple.com/Q123", "https://wns2-par02p.notify.windows.com/w/?token=x"]
    bad = ["http://fcm.googleapis.com/x", "https://evil.example/x", "https://fcm.googleapis.com.evil.io/x", "https://127.0.0.1/x",
           "https://fcm.googleapis.com:8443/x", "https://user:p@fcm.googleapis.com/x", "https://169.254.169.254/latest", "ftp://fcm.googleapis.com/x", ""]
    assert all(w.endpoint_ok(u) for u in ok), [u for u in ok if not w.endpoint_ok(u)]
    assert not any(w.endpoint_ok(u) for u in bad), [u for u in bad if w.endpoint_ok(u)]

def test_send_against_local_mock_push_service():
    """خادم HTTP محلي يلعب دور خدمة الدفع: نتأكد من الرؤوس والجسم ونفك التشفير بمفتاح المتصفح الوهمي."""
    import threading, http.server
    ua = ec.generate_private_key(ec.SECP256R1()); auth = w.b64u(os.urandom(16)); p256 = w.b64u(w._pub_bytes(ua.public_key()))
    got = {}
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(s):
            got["h"] = dict(s.headers); got["b"] = s.rfile.read(int(s.headers["Content-Length"])); s.send_response(201); s.end_headers()
        def log_message(*a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = f"http://127.0.0.1:{srv.server_port}/push/1"
    p, _ = w.generate_keys(); v = w.Vapid(p, "mailto:a@b.co")
    os.environ["PUSH_ALLOWED_HOSTS"] = "127.0.0.1"
    # الاختبار المحلي يتجاوز فحص https فقط عبر الاستدعاء المباشر للطبقات الدنيا
    import requests
    payload = {"title": "t", "body": "جاء دورك", "url": "/track"}
    body = w.encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), p256, auth)
    r = requests.post(ep, data=body, headers={**v.headers(ep), "Content-Encoding": "aes128gcm", "TTL": "3600", "Urgency": "high", "Content-Type": "application/octet-stream"})
    srv.shutdown()
    assert r.status_code == 201 and got["h"]["Content-Encoding"] == "aes128gcm" and got["h"]["Authorization"].startswith("vapid t=")
    assert json.loads(w.decrypt(got["b"], ua, auth)) == payload
    # send() نفسها ترفض عنواناً خارج القائمة المسموحة (http أو localhost) دون أي اتصال
    assert w.send(v, ep, p256, auth, payload)[0] == 400

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("PASS", n)

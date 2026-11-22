"""
دورك (DAWRAK) - الباك اند (FastAPI + PostgreSQL)

فهرس الملف:
  1. الإعدادات والاتصال بقاعدة البيانات
  2. أدوات مساعدة (الأخطاء، التوكن، اتصال القاعدة الكسول)
  3. الصفحة الرئيسية
  4. الدخول والتسجيل (مريض / طبيب / أدمن / Google)
  5. حساب المريض (الملف، أفراد العيلة، إلغاء وتأجيل الحجز، التحاليل والأشعة)
  6. الأطباء والحجز العام
  7. لوحة الطبيب (الطابور، التقرير والروشتة، ملف المريض)
  8. لوحة الأدمن (إدارة الأطباء)
  9. موظف الاستقبال (الطابور الموحّد)، وإدارته من الأدمن، وسجل العمليات (queue_audit)

إدارة الطابور (استدعاء/بدء/إنهاء/تخطي/إلغاء/ترتيب) لموظف الاستقبال فقط؛ الطبيب يقرأ الطابور ويكتب التقرير.
قواعد الخصوصية: تقرير الكشف والملفات لا يراها إلا المريض صاحب الحجز وطبيب الحجز فقط.

نسخة Vercel: لا WebSocket (الواجهة تحدّث نفسها كل 5 ثوانٍ)، والـ Pool يُنشأ عند أول طلب،
وتنضيف الحجوزات القديمة تعمل عند الطلب بدل مهمة الخلفية.
"""
import os, re, json, base64, asyncio, secrets, time, traceback
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg, bcrypt, jwt
from fastapi import FastAPI, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/clinic_queue")
# سر التوقيع: لا يوجد قيمة افتراضية معروفة. لو JWT_SECRET غير مضبوط نولّد سراً عشوائياً لهذه العملية فقط
# (آمن، لكن تنتهي الجلسات عند إعادة التشغيل) — اضبط JWT_SECRET في الإنتاج.
SECRET = os.getenv("JWT_SECRET") or secrets.token_urlsafe(48)
if not os.getenv("JWT_SECRET"):
    print("WARNING: JWT_SECRET is not set; using a random per-process secret.")
PORT = int(os.getenv("PORT", "3000"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")


class LazyPool:
    """
    اتصال القاعدة يُنشأ عند أول استخدام (لا نعتمد على lifespan في Vercel).
      - statement_cache_size=0          : متوافق مع pgbouncer / Neon pooler
      - max_size=3                      : كل نسخة دالة لها Pool صغير خاص بها
      - max_inactive_connection_lifetime: نغلق الاتصالات الخاملة قبل أن تقطعها القاعدة
      - لو تغيّر الـ event loop (نسخة جديدة) نعيد إنشاء الـ Pool
    """
    def __init__(self):
        self._task, self._loop = None, None

    async def get(self):
        loop = asyncio.get_running_loop()
        if self._task is None or self._loop is not loop:
            self._loop = loop
            self._task = asyncio.ensure_future(asyncpg.create_pool(
                DSN, min_size=1, max_size=3, statement_cache_size=0, command_timeout=30, max_inactive_connection_lifetime=30), loop=loop)
        try:
            return await self._task
        except Exception:
            self._task = None  # لو فشل الاتصال نحاول من جديد في الطلب التالي
            raise

    async def fetch(self, *a): return await (await self.get()).fetch(*a)
    async def fetchrow(self, *a): return await (await self.get()).fetchrow(*a)
    async def fetchval(self, *a): return await (await self.get()).fetchval(*a)
    async def execute(self, *a): return await (await self.get()).execute(*a)
    def acquire(self): return _Acquire(self)


class _Acquire:
    """async with pool.acquire() as c  (يعمل مع LazyPool)"""
    def __init__(self, lp):
        self.lp, self.cm = lp, None

    async def __aenter__(self):
        self.cm = (await self.lp.get()).acquire()
        return await self.cm.__aenter__()

    async def __aexit__(self, *exc):
        return await self.cm.__aexit__(*exc)


pool = LazyPool()
_last_cleanup = 0.0
tries: dict = {}


class Fail(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


async def changed():
    """لا بث لحظي في Vercel: الواجهة تعمل تحديثاً دورياً (Polling) كل 5 ثوانٍ."""
    return


async def cleanup_if_due():
    """إلغاء حجوزات الأيام السابقة غير المنتهية (بديل المهمة الساعية): مرة كل ساعة كحد أقصى لكل نسخة."""
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    _last_cleanup = time.time()
    try:
        await pool.execute("UPDATE bookings SET status='Cancelled' WHERE booking_date<CURRENT_DATE AND status IN ('Waiting','In Progress')")
    except Exception as e:
        print(e)


app = FastAPI(docs_url=None, redoc_url=None)


@app.exception_handler(Fail)
async def on_fail(r, e):
    return JSONResponse({"error": e.message}, status_code=e.status)


@app.exception_handler(Exception)
async def on_error(r, e):
    traceback.print_exception(e)
    return JSONResponse({"error": "حدث خطأ في الخادم"}, status_code=500)


async def body(r: Request):
    try:
        b = await r.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ---------- helpers ----------
def cnt(st=None):
    f = f"AND b.status='{st}'" if st else ""
    return f"(SELECT count(*) FROM bookings b WHERE b.doctor_id=d.id AND b.booking_date=CURRENT_DATE {f})::int"


STATS = f"""SELECT d.id,d.name,d.specialty,d.description,d.profile_image,d.username,d.average_consultation_time AS avg,
  d.is_active,d.is_accepting_bookings AS accepting,{cnt('Waiting')} AS waiting,{cnt('In Progress')} AS in_progress,
  {cnt('Completed')} AS completed,{cnt()} AS total FROM doctors d"""


def view(r):
    return {"id": r["id"], "name": r["name"], "specialty": r["specialty"], "description": r["description"],
            "profile_image": r["profile_image"], "avg": r["avg"], "accepting": r["accepting"], "waiting": r["waiting"],
            "in_progress": r["in_progress"], "completed": r["completed"],
            "status": "busy" if r["in_progress"] else "available" if r["accepting"] else "offline"}


def adm(r):
    return {**view(r), "username": r["username"], "is_active": r["is_active"], "total": r["total"]}


def auth(role):
    async def dep(request: Request):
        try:
            u = jwt.decode(request.headers.get("authorization", "").replace("Bearer ", ""), SECRET, algorithms=["HS256"])
            if u["role"] != role:
                raise ValueError
        except Exception:
            raise Fail(401, "غير مصرح، سجّل الدخول من جديد")
        # التحقق من أن الحساب ما زال موجوداً وفعّالاً (حتى لو التوكن سليم)
        ok = await pool.fetchval({"doctor": "SELECT 1 FROM doctors WHERE id=$1 AND is_active",
                                  "admin": "SELECT 1 FROM admins WHERE id=$1",
                                  "patient": "SELECT 1 FROM patients WHERE id=$1",
                                  "receptionist": "SELECT 1 FROM receptionists r JOIN doctors d ON d.id=r.doctor_id WHERE r.id=$1 AND r.is_active AND d.is_active"}[role], u["id"])
        if not ok:
            raise Fail(401, "الحساب غير موجود أو معطّل")
        return u
    return dep


# ---------- pages & realtime ----------
# كل بوابة لها رابط صفحة مستقل. الصفحة نفسها قالب فارغ لا يحتوي أي بيانات؛ الحماية الحقيقية في الـ API (auth(role)).
PAGE_HEADERS = {"Cross-Origin-Opener-Policy": "same-origin-allow-popups", "Cache-Control": "no-store",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}


def shell(private=False):
    async def page():
        h = {**PAGE_HEADERS, **({"X-Robots-Tag": "noindex, nofollow"} if private else {})}
        return FileResponse(Path(__file__).parent / "index.html", headers=h)
    return page


app.add_api_route("/", shell(), methods=["GET"], include_in_schema=False)
for _p in ("/doctors", "/patient/login", "/patient/register", "/patient/dashboard"):
    app.add_api_route(_p, shell(), methods=["GET"], include_in_schema=False)
for _p in ("/admin/login", "/admin/dashboard", "/doctor/login", "/doctor/dashboard", "/doctor/profile", "/receptionist/login", "/receptionist/dashboard"):
    app.add_api_route(_p, shell(True), methods=["GET"], include_in_schema=False)
for _r, _to in (("/patient", "/patient/login"), ("/doctor", "/doctor/login"), ("/admin", "/admin/login"), ("/receptionist", "/receptionist/login")):
    app.add_api_route(_r, (lambda to: (lambda: RedirectResponse(to)))(_to), methods=["GET"], include_in_schema=False)


# ---------- login (بوابة منفصلة لكل دور؛ الدور يحدده المسار وليس ما يرسله العميل) ----------
LOGIN_SQL = {"doctor": "SELECT id,password_hash,is_active FROM doctors WHERE username=$1",
             "admin": "SELECT id,password_hash,TRUE AS is_active FROM admins WHERE username=$1",
             "patient": "SELECT id,password_hash,TRUE AS is_active FROM patients WHERE login_id=$1",
             "receptionist": "SELECT r.id,r.password_hash,(r.is_active AND d.is_active) AS is_active FROM receptionists r JOIN doctors d ON d.id=r.doctor_id WHERE r.email=$1"}
DUMMY_HASH = bcrypt.hashpw(b"dawrak-dummy", bcrypt.gensalt(10)).decode()  # لتساوي زمن الرد سواء وُجد المستخدم أم لا


async def do_login(request: Request, role: str):
    if role == "patient":
        raise Fail(410, "دخول المرضى عن طريق حساب Google فقط")
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    key = f"{ip}:{role}"
    n, at = tries.get(key, (0, time.time()))
    if time.time() - at > 900:
        n, at = 0, time.time()
    if n >= 10:
        raise Fail(429, "محاولات كثيرة، حاول بعد 15 دقيقة")
    b = await body(request)
    username, password = b.get("username"), b.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        raise Fail(400, "أدخل اسم المستخدم وكلمة المرور")
    u = await pool.fetchrow(LOGIN_SQL[role], username.strip().lower())
    good = await asyncio.to_thread(bcrypt.checkpw, password.encode()[:72], (u["password_hash"] if u else DUMMY_HASH).encode())
    if not (u and good):
        tries[key] = (n + 1, at)
        raise Fail(401, "بيانات الدخول غير صحيحة")
    if not u["is_active"]:
        raise Fail(403, "هذا الحساب معطّل")
    tries.pop(key, None)
    return {"token": make_token(role, u["id"]), "role": role}


@app.post("/api/admin/login")
async def admin_login(request: Request):
    return await do_login(request, "admin")


@app.post("/api/doctor/login")
async def doctor_login(request: Request):
    return await do_login(request, "doctor")


@app.post("/api/patient/login")
async def patient_login(request: Request):
    return await do_login(request, "patient")


@app.post("/api/receptionist/login")
async def receptionist_login(request: Request):
    return await do_login(request, "receptionist")


def make_token(role, uid):
    return jwt.encode({"role": role, "id": uid, "exp": datetime.now(timezone.utc) + timedelta(hours=12)}, SECRET, algorithm="HS256")


# ---------- patient accounts ----------
CHRONIC = ["السكري", "ضغط الدم", "أمراض القلب", "الربو", "أمراض الكلى", "أمراض الكبد", "الغدة الدرقية",
           "الروماتيزم والمفاصل", "الصرع", "الأنيميا", "حساسية الأدوية"]


def vpat(b):
    g = lambda k: str(b.get(k) or "").strip()
    ch = b.get("chronic")
    o = {"name": g("name"), "phone": re.sub(r"[\s-]", "", g("phone")), "wa": re.sub(r"[\s-]", "", g("whatsapp")),
         "other": g("other_conditions"), "notes": g("notes"), "chronic": [x for x in CHRONIC if isinstance(ch, list) and x in ch]}
    try:
        o["age"] = int(b.get("age"))
    except Exception:
        o["age"] = 0
    if not 2 <= len(o["name"]) <= 60: raise Fail(400, "اكتب اسمك (حرفان على الأقل)")
    if not re.fullmatch(r"\+?\d{8,15}", o["phone"]): raise Fail(400, "رقم الهاتف غير صحيح")
    if not re.fullmatch(r"\+?\d{8,15}", o["wa"]): raise Fail(400, "رقم الواتساب غير صحيح")
    if not 1 <= o["age"] <= 120: raise Fail(400, "العمر غير صحيح")
    if len(o["other"]) > 300: raise Fail(400, "حقل الأمراض الأخرى طويل (300 حرف كحد أقصى)")
    if len(o["notes"]) > 1000: raise Fail(400, "الملاحظات طويلة (1000 حرف كحد أقصى)")
    return o


@app.post("/api/register", status_code=201)
async def register(request: Request):
    raise Fail(410, "إنشاء حساب المرضى عن طريق Google فقط")
    b = await body(request)
    o = vpat(b)
    lid, pw = str(b.get("login_id") or "").strip().lower(), str(b.get("password") or "")
    # الإيميل لا يُقبل كاسم مستخدم يدوي: إيميلات المرضى تُسجَّل عبر Google فقط (مؤكَّدة)،
    # وإلا يستطيع أي شخص حجز إيميل غيره ثم الاستيلاء على حسابه عند دخوله بجوجل.
    if "@" in lid:
        raise Fail(400, "للتسجيل بالإيميل استخدم زر Google، أو اختر اسم مستخدم بدون @")
    if not re.fullmatch(r"[a-z0-9_.]{3,30}", lid):
        raise Fail(400, "اسم المستخدم 3-30 حرفاً: حروف إنجليزية صغيرة وأرقام و _ . فقط")
    if len(pw) < 6: raise Fail(400, "كلمة المرور 6 أحرف على الأقل")
    try:
        uid = await pool.fetchval("""INSERT INTO patients(name,phone,whatsapp,age,login_id,password_hash,chronic_conditions,other_conditions,notes)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""", o["name"], o["phone"], o["wa"], o["age"], lid,
                                  await asyncio.to_thread(hashpw, pw), o["chronic"], o["other"], o["notes"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "اسم المستخدم أو الإيميل مستخدم بالفعل")
    return {"token": make_token("patient", uid), "role": "patient"}


# ---------- Google Sign-In (للمرضى) ----------
@app.get("/api/config")
async def config():
    return {"google_client_id": GOOGLE_CLIENT_ID}


def verify_google(token):
    from google.oauth2 import id_token
    from google.auth.transport import requests as greq
    return id_token.verify_oauth2_token(token, greq.Request(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)


@app.post("/api/google")
async def google_login(request: Request):
    if not GOOGLE_CLIENT_ID:
        raise Fail(503, "تسجيل جوجل غير مفعّل على هذا السيرفر")
    b = await body(request)
    try:
        info = await asyncio.to_thread(verify_google, str(b.get("credential") or ""))
    except ImportError:
        raise Fail(500, "ثبّت المكتبة على السيرفر: pip install google-auth requests")
    except Exception:
        raise Fail(401, "تعذر التحقق من حساب جوجل")
    email = str(info.get("email") or "").lower()
    if not email or not info.get("email_verified"):
        raise Fail(401, "إيميل جوجل غير مؤكد")
    uid = await pool.fetchval("SELECT id FROM patients WHERE login_id=$1", email)
    if uid:
        return {"token": make_token("patient", uid), "role": "patient"}
    st = jwt.encode({"typ": "gsignup", "email": email, "exp": datetime.now(timezone.utc) + timedelta(minutes=15)}, SECRET, algorithm="HS256")
    return {"needs_signup": True, "signup_token": st, "email": email, "name": str(info.get("name") or "")[:60]}


@app.post("/api/google/register", status_code=201)
async def google_register(request: Request):
    b = await body(request)
    try:
        t = jwt.decode(str(b.get("signup_token") or ""), SECRET, algorithms=["HS256"])
        if t.get("typ") != "gsignup":
            raise ValueError
    except Exception:
        raise Fail(401, "انتهت صلاحية الجلسة، سجّل بجوجل من جديد")
    o = vpat(b)
    try:
        uid = await pool.fetchval("""INSERT INTO patients(name,phone,whatsapp,age,login_id,password_hash,chronic_conditions,other_conditions,notes)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""", o["name"], o["phone"], o["wa"], o["age"], t["email"],
                                  await asyncio.to_thread(hashpw, secrets.token_urlsafe(32)), o["chronic"], o["other"], o["notes"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "هذا الحساب مسجّل بالفعل، سجّل الدخول")
    return {"token": make_token("patient", uid), "role": "patient"}


@app.get("/api/me")
async def me(u=Depends(auth("patient"))):
    p = await pool.fetchrow("SELECT name,phone,whatsapp,age,login_id,chronic_conditions,other_conditions,notes FROM patients WHERE id=$1", u["id"])
    if not p:
        raise Fail(401, "الحساب غير موجود")
    rows = await pool.fetch("""SELECT b.id,b.queue_number,b.status,b.booking_date,b.created_at,b.completed_at,r.report,r.medications,r.follow_up_days,(b.booking_date + r.follow_up_days) AS follow_up_date,b.member_id,(b.called_at IS NOT NULL AND b.status='Waiting') AS called,m.name AS member_name,m.relation,d.name AS doctor_name,
      d.specialty,d.average_consultation_time AS avg,
      CASE WHEN b.status='Waiting' THEN (SELECT count(*) FROM bookings x WHERE x.doctor_id=b.doctor_id AND x.booking_date=b.booking_date
        AND x.status='Waiting' AND x.queue_number<b.queue_number)::int ELSE 0 END AS ahead
      FROM bookings b JOIN doctors d ON d.id=b.doctor_id LEFT JOIN visit_reports r ON r.booking_id=b.id LEFT JOIN family_members m ON m.id=b.member_id WHERE b.patient_id=$1 ORDER BY b.booking_date DESC,b.id DESC LIMIT 100""", u["id"])
    fl = await pool.fetch("SELECT id,booking_id,kind,filename,mime,size FROM booking_files WHERE booking_id=ANY($1::int[]) ORDER BY id", [r["id"] for r in rows])
    fam = await pool.fetch("SELECT id,name,relation,age,chronic_conditions,other_conditions,notes FROM family_members WHERE patient_id=$1 AND NOT archived ORDER BY id", u["id"])
    return {"profile": dict(p), "family": [dict(x) for x in fam],
            "bookings": [{**dict(r), "medications": jl(r["medications"]), "files": [dict(f) for f in fl if f["booking_id"] == r["id"]]} for r in rows]}


@app.put("/api/me")
async def update_me(request: Request, u=Depends(auth("patient"))):
    o = vpat(await body(request))
    await pool.execute("UPDATE patients SET name=$2,phone=$3,whatsapp=$4,age=$5,chronic_conditions=$6,other_conditions=$7,notes=$8 WHERE id=$1",
                       u["id"], o["name"], o["phone"], o["wa"], o["age"], o["chronic"], o["other"], o["notes"])
    return {"ok": True}


# ---------- أفراد العيلة ----------
RELATIONS = ["ابن", "ابنة", "الأب", "الأم", "الزوج", "الزوجة", "أخ", "أخت", "آخر"]


def vmember(b):
    g = lambda k: str(b.get(k) or "").strip()
    ch = b.get("chronic")
    o = {"name": g("name"), "relation": g("relation"), "other": g("other_conditions"), "notes": g("notes"),
         "chronic": [x for x in CHRONIC if isinstance(ch, list) and x in ch]}
    try:
        o["age"] = int(b.get("age"))
    except Exception:
        o["age"] = -1
    if not 2 <= len(o["name"]) <= 60: raise Fail(400, "اكتب اسم الفرد (حرفان على الأقل)")
    if o["relation"] not in RELATIONS: raise Fail(400, "اختر صلة القرابة")
    if not 0 <= o["age"] <= 120: raise Fail(400, "العمر غير صحيح")
    if len(o["other"]) > 300: raise Fail(400, "حقل الأمراض الأخرى طويل (300 حرف كحد أقصى)")
    if len(o["notes"]) > 1000: raise Fail(400, "الملاحظات طويلة (1000 حرف كحد أقصى)")
    return o


@app.get("/api/me/family")
async def my_family(u=Depends(auth("patient"))):
    rows = await pool.fetch("SELECT id,name,relation,age FROM family_members WHERE patient_id=$1 AND NOT archived ORDER BY id", u["id"])
    return [dict(r) for r in rows]


@app.post("/api/me/family", status_code=201)
async def add_member(request: Request, u=Depends(auth("patient"))):
    o = vmember(await body(request))
    if await pool.fetchval("SELECT count(*) FROM family_members WHERE patient_id=$1 AND NOT archived", u["id"]) >= 10:
        raise Fail(409, "الحد الأقصى 10 أفراد")
    mid = await pool.fetchval("INSERT INTO family_members(patient_id,name,relation,age,chronic_conditions,other_conditions,notes) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                              u["id"], o["name"], o["relation"], o["age"], o["chronic"], o["other"], o["notes"])
    return {"id": mid}


@app.put("/api/me/family/{mid}")
async def edit_member(mid: int, request: Request, u=Depends(auth("patient"))):
    o = vmember(await body(request))
    r = await pool.fetchval("UPDATE family_members SET name=$3,relation=$4,age=$5,chronic_conditions=$6,other_conditions=$7,notes=$8 WHERE id=$1 AND patient_id=$2 AND NOT archived RETURNING id",
                            mid, u["id"], o["name"], o["relation"], o["age"], o["chronic"], o["other"], o["notes"])
    if not r:
        raise Fail(404, "الفرد غير موجود")
    return {"ok": True}


@app.delete("/api/me/family/{mid}")
async def del_member(mid: int, u=Depends(auth("patient"))):  # إخفاء فقط حتى يبقى سجل زياراته
    await pool.execute("UPDATE family_members SET archived=TRUE WHERE id=$1 AND patient_id=$2", mid, u["id"])
    return {"ok": True}


# ---------- إلغاء وتأجيل الحجز ----------
@app.post("/api/me/bookings/{bid}/cancel")
async def cancel_booking(bid: int, u=Depends(auth("patient"))):
    r = await pool.fetchval("UPDATE bookings SET status='Cancelled' WHERE id=$1 AND patient_id=$2 AND status='Waiting' AND booking_date=CURRENT_DATE RETURNING id", bid, u["id"])
    if not r:
        raise Fail(409, "لا يمكن إلغاء هذا الحجز (ربما بدأ الكشف بالفعل)")
    await changed()
    return {"ok": True}


@app.post("/api/me/bookings/{bid}/postpone")
async def postpone_booking(bid: int, u=Depends(auth("patient"))):  # التأجيل = نقل الحجز لآخر طابور اليوم
    async with pool.acquire() as c, c.transaction():
        await c.fetchval("SELECT 1 FROM doctors WHERE id=(SELECT doctor_id FROM bookings WHERE id=$1) FOR UPDATE", bid)
        b = await c.fetchrow("SELECT doctor_id,queue_number FROM bookings WHERE id=$1 AND patient_id=$2 AND status='Waiting' AND booking_date=CURRENT_DATE", bid, u["id"])
        if not b:
            raise Fail(409, "لا يمكن تأجيل هذا الحجز")
        mx = await c.fetchval("SELECT MAX(queue_number) FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE", b["doctor_id"])
        if not await c.fetchval("SELECT 1 FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='Waiting' AND queue_number>$2", b["doctor_id"], b["queue_number"]):
            raise Fail(409, "أنت بالفعل آخر شخص في الطابور")
        await c.execute("UPDATE bookings SET queue_number=$2,called_at=NULL WHERE id=$1", bid, mx + 1)
    await changed()
    return {"ok": True}


# ---------- تحاليل وأشعة ----------
SIG = {"application/pdf": b"%PDF", "image/jpeg": b"\xff\xd8", "image/png": b"\x89PNG", "image/webp": b"RIFF"}


def file_resp(f):
    return Response(f["data"], media_type=f["mime"], headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(f["filename"]), "X-Content-Type-Options": "nosniff"})


@app.post("/api/me/bookings/{bid}/files", status_code=201)
async def add_file(bid: int, request: Request, u=Depends(auth("patient"))):
    b = await body(request)
    kind = b.get("kind") if b.get("kind") in ("lab", "xray", "other") else "other"
    mime, name = str(b.get("mime") or ""), str(b.get("filename") or "file")[:120]
    if mime not in SIG:
        raise Fail(400, "الملفات المسموحة: صور (JPG/PNG/WebP) أو PDF")
    try:
        data = base64.b64decode(str(b.get("data") or ""), validate=True)
    except Exception:
        raise Fail(400, "الملف غير صالح")
    if not 0 < len(data) <= 3 * 1024 * 1024:
        raise Fail(400, "حجم الملف يجب ألا يتجاوز 3 ميجا")
    if not data.startswith(SIG[mime]):
        raise Fail(400, "محتوى الملف لا يطابق نوعه")
    if not await pool.fetchval("SELECT 1 FROM bookings WHERE id=$1 AND patient_id=$2 AND status IN ('Waiting','In Progress')", bid, u["id"]):
        raise Fail(403, "لا يمكنك رفع ملفات لهذا الحجز")
    if await pool.fetchval("SELECT count(*) FROM booking_files WHERE booking_id=$1", bid) >= 10:
        raise Fail(409, "الحد الأقصى 10 ملفات لكل حجز")
    fid = await pool.fetchval("INSERT INTO booking_files(booking_id,kind,filename,mime,size,data) VALUES($1,$2,$3,$4,$5,$6) RETURNING id", bid, kind, name, mime, len(data), data)
    await changed()
    return {"id": fid}


@app.get("/api/me/files/{fid}")
async def my_file(fid: int, u=Depends(auth("patient"))):
    f = await pool.fetchrow("SELECT f.filename,f.mime,f.data FROM booking_files f JOIN bookings b ON b.id=f.booking_id WHERE f.id=$1 AND b.patient_id=$2", fid, u["id"])
    if not f:
        raise Fail(404, "الملف غير موجود")
    return file_resp(f)


@app.delete("/api/me/files/{fid}")
async def del_file(fid: int, u=Depends(auth("patient"))):
    r = await pool.fetchval("DELETE FROM booking_files f USING bookings b WHERE f.id=$1 AND b.id=f.booking_id AND b.patient_id=$2 AND b.status IN ('Waiting','In Progress') RETURNING f.id", fid, u["id"])
    if not r:
        raise Fail(404, "لا يمكن حذف هذا الملف")
    await changed()
    return {"ok": True}


# ---------- public ----------
@app.get("/api/doctors")
async def doctors():
    await cleanup_if_due()
    return [view(r) for r in await pool.fetch(STATS + " WHERE d.is_active ORDER BY d.id")]


async def ticket(bid):
    t = await pool.fetchrow("""SELECT b.id,b.queue_number,b.status,d.name AS doctor_name,d.specialty,d.average_consultation_time AS avg,
      CASE WHEN b.status='Waiting' THEN (SELECT count(*) FROM bookings x WHERE x.doctor_id=b.doctor_id AND x.booking_date=b.booking_date
        AND x.status='Waiting' AND x.queue_number<b.queue_number)::int ELSE 0 END AS ahead
      FROM bookings b JOIN doctors d ON d.id=b.doctor_id WHERE b.id=$1""", bid)
    return dict(t) if t else None


@app.get("/api/bookings/{bid}")
async def get_booking(bid: int):
    t = await ticket(bid)
    if not t:
        raise Fail(404, "الحجز غير موجود")
    return t


@app.post("/api/bookings", status_code=201)
async def create_booking(request: Request, u=Depends(auth("patient"))):
    b = await body(request)
    try:
        did = int(b.get("doctor_id"))
    except Exception:
        raise Fail(400, "اختر طبيباً")
    p = await pool.fetchrow("SELECT name,phone FROM patients WHERE id=$1", u["id"])
    if not p:
        raise Fail(401, "الحساب غير موجود")
    name, phone, mid = p["name"], p["phone"], None
    if b.get("member_id") not in (None, "", 0):
        try:
            mid = int(b.get("member_id"))
        except Exception:
            raise Fail(400, "فرد غير صحيح")
        mem = await pool.fetchrow("SELECT name FROM family_members WHERE id=$1 AND patient_id=$2 AND NOT archived", mid, u["id"])
        if not mem:
            raise Fail(404, "الفرد غير موجود")
        name = mem["name"]
    async with pool.acquire() as c, c.transaction():
        d = await c.fetchrow("SELECT is_active,is_accepting_bookings FROM doctors WHERE id=$1 FOR UPDATE", did)  # يسلسل الحجوزات المتزامنة
        if not d or not d["is_active"]:
            raise Fail(404, "الطبيب غير متاح")
        if not d["is_accepting_bookings"]:
            raise Fail(409, "الطبيب أغلق استقبال الحجوزات حالياً")
        if await c.fetchval("SELECT 1 FROM bookings WHERE doctor_id=$1 AND patient_id=$2 AND member_id IS NOT DISTINCT FROM $3::int AND booking_date=CURRENT_DATE AND status IN ('Waiting','In Progress')", did, u["id"], mid):
            raise Fail(409, "يوجد حجز نشط لهذا الشخص عند هذا الطبيب اليوم")
        bid = await c.fetchval("""INSERT INTO bookings(doctor_id,patient_name,patient_phone,patient_id,member_id,queue_number)
          SELECT $1::int,$2::text,$3::text,$4::int,$5::int,COALESCE(MAX(queue_number),0)+1 FROM bookings WHERE doctor_id=$1::int AND booking_date=CURRENT_DATE RETURNING id""", did, name, phone, u["id"], mid)
    await changed()
    return await ticket(bid)


# ---------- doctor ----------
async def dq(did):
    """حالة طابور طبيب (يستعملها الطبيب للقراءة وموظف الاستقبال للإدارة): الحالي + المنتظرون (موحّدون أونلاين/حضور) + منتهي اليوم."""
    d = await pool.fetchrow(STATS + " WHERE d.id=$1", did)
    if not d:
        raise Fail(404, "غير موجود")
    rows = [dict(x) for x in await pool.fetch("""SELECT id,patient_id,member_id,queue_number,patient_name,patient_phone,status,created_at,source,
      (called_at IS NOT NULL) AS called,EXISTS(SELECT 1 FROM visit_reports v WHERE v.booking_id=bookings.id) AS has_report,
      (SELECT r.name FROM receptionists r WHERE r.id=bookings.created_by_receptionist) AS added_by,
      (SELECT count(*) FROM booking_files f WHERE f.booking_id=bookings.id)::int AS files FROM bookings
      WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status IN ('Waiting','In Progress') ORDER BY queue_number""", did)]
    done = [dict(x) for x in await pool.fetch("""SELECT id,queue_number,patient_name,source,completed_at,
      EXISTS(SELECT 1 FROM visit_reports v WHERE v.booking_id=bookings.id) AS has_report FROM bookings
      WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='Completed' ORDER BY completed_at DESC NULLS LAST LIMIT 20""", did)]
    return {"doctor": view(d), "current": next((x for x in rows if x["status"] == "In Progress"), None),
            "waiting": [x for x in rows if x["status"] == "Waiting"], "done": done}


@app.get("/api/doctor/queue")
async def d_queue(u=Depends(auth("doctor"))):
    return await dq(u["id"])


def jl(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def vreport(b):
    rep = str(b.get("report") or "").strip()
    if not 3 <= len(rep) <= 3000:
        raise Fail(400, "اكتب تقرير الكشف (3 أحرف على الأقل)")
    meds = []
    for m in (b.get("medications") if isinstance(b.get("medications"), list) else [])[:30]:
        if not isinstance(m, dict):
            continue
        g = lambda k, n=100: str(m.get(k) or "").strip()[:n]
        if g("name"):
            meds.append({"name": g("name"), "dose": g("dose"), "freq": g("freq"), "notes": g("notes", 200)})
    fu = b.get("follow_up_days")
    if fu in (None, "", False):
        fu = None
    else:
        try:
            fu = int(fu)
        except Exception:
            fu = 0
        if not 1 <= fu <= 365:
            raise Fail(400, "عدد أيام الاستشارة بين 1 و365")
    return rep, meds, fu


@app.put("/api/doctor/report/{bid}")
async def d_report(bid: int, request: Request, u=Depends(auth("doctor"))):  # الطبيب يكتب تقرير كشفه هو فقط
    rep, meds, fu = vreport(await body(request))
    if not await pool.fetchval("SELECT 1 FROM bookings WHERE id=$1 AND doctor_id=$2 AND status IN ('In Progress','Completed')", bid, u["id"]):
        raise Fail(403, "لا يمكنك كتابة تقرير لهذا الكشف")
    await pool.execute("""INSERT INTO visit_reports(booking_id,report,medications,follow_up_days) VALUES($1,$2,$3::jsonb,$4)
      ON CONFLICT (booking_id) DO UPDATE SET report=EXCLUDED.report,medications=EXCLUDED.medications,
      follow_up_days=EXCLUDED.follow_up_days,updated_at=now()""", bid, rep, json.dumps(meds, ensure_ascii=False), fu)
    return {"ok": True}


@app.get("/api/doctor/report/{bid}")
async def d_report_get(bid: int, u=Depends(auth("doctor"))):  # لتعديل تقرير كتبه الطبيب نفسه
    r = await pool.fetchrow("SELECT r.report,r.medications,r.follow_up_days FROM visit_reports r JOIN bookings b ON b.id=r.booking_id WHERE r.booking_id=$1 AND b.doctor_id=$2", bid, u["id"])
    if not r:
        return {"report": None, "medications": [], "follow_up_days": None}
    return {"report": r["report"], "medications": jl(r["medications"]), "follow_up_days": r["follow_up_days"]}


@app.get("/api/doctor/patient/{pid}")
async def d_patient(pid: int, m: int = 0, u=Depends(auth("doctor"))):  # m = فرد العيلة (0 = المريض نفسه)
    visits = await pool.fetch("""SELECT b.id,b.booking_date,b.status,r.report,r.medications,r.follow_up_days,(b.booking_date + r.follow_up_days) AS follow_up_date
      FROM bookings b LEFT JOIN visit_reports r ON r.booking_id=b.id
      WHERE b.doctor_id=$1 AND b.patient_id=$2 AND COALESCE(b.member_id,0)=$3 ORDER BY b.booking_date DESC,b.id DESC LIMIT 20""", u["id"], pid, m)
    if not visits:
        raise Fail(403, "لا يمكنك عرض ملف مريض ليس لديه حجز عندك")
    own = await pool.fetchrow("SELECT name,phone,whatsapp,age,chronic_conditions,other_conditions,notes FROM patients WHERE id=$1", pid)
    p = dict(own)
    if m:
        mem = await pool.fetchrow("SELECT name,relation,age,chronic_conditions,other_conditions,notes FROM family_members WHERE id=$1 AND patient_id=$2", m, pid)
        if mem:
            p = {**p, **dict(mem), "guardian": own["name"]}
    fl = await pool.fetch("SELECT id,booking_id,kind,filename,mime,size FROM booking_files WHERE booking_id=ANY($1::int[]) ORDER BY id", [v["id"] for v in visits])
    return {"profile": p, "visits": [{**dict(v), "medications": jl(v["medications"]), "files": [dict(f) for f in fl if f["booking_id"] == v["id"]]} for v in visits]}


@app.get("/api/doctor/files/{fid}")
async def d_file(fid: int, u=Depends(auth("doctor"))):  # ملفات الحجوزات الخاصة بهذا الطبيب فقط
    f = await pool.fetchrow("SELECT f.filename,f.mime,f.data FROM booking_files f JOIN bookings b ON b.id=f.booking_id WHERE f.id=$1 AND b.doctor_id=$2", fid, u["id"])
    if not f:
        raise Fail(404, "الملف غير موجود")
    return file_resp(f)


# إدارة الطابور (استدعاء/بدء/إنهاء) من اختصاص موظف الاستقبال فقط، والرفض هنا من الباك اند وليس من الواجهة.
@app.post("/api/doctor/start")
@app.post("/api/doctor/finish")
async def d_queue_locked(u=Depends(auth("doctor"))):
    raise Fail(403, "إدارة الطابور من اختصاص موظف الاستقبال")


@app.post("/api/doctor/accepting")
async def d_accepting(request: Request, u=Depends(auth("doctor"))):
    b = await body(request)
    await pool.execute("UPDATE doctors SET is_accepting_bookings=$2 WHERE id=$1", u["id"], b.get("accepting") is True)
    await changed()
    return await dq(u["id"])


# ---------- الملف الشخصي للطبيب ----------
@app.get("/api/doctor/profile")
async def d_profile(u=Depends(auth("doctor"))):
    d = await pool.fetchrow("""SELECT name,specialty,description,profile_image,username,average_consultation_time AS avg,
      is_accepting_bookings AS accepting FROM doctors WHERE id=$1""", u["id"])
    s = await pool.fetchrow("""SELECT count(*)::int AS total,
      count(*) FILTER (WHERE status='Completed')::int AS completed,
      count(*) FILTER (WHERE status='Cancelled')::int AS cancelled,
      count(*) FILTER (WHERE booking_date=CURRENT_DATE AND status IN ('Waiting','In Progress'))::int AS waiting_today,
      count(*) FILTER (WHERE booking_date=CURRENT_DATE AND status='Completed')::int AS done_today,
      count(*) FILTER (WHERE booking_date>=CURRENT_DATE-6 AND status='Completed')::int AS week
      FROM bookings WHERE doctor_id=$1""", u["id"])
    rows = await pool.fetch("""SELECT id,queue_number,patient_name,status,booking_date FROM bookings
      WHERE doctor_id=$1 ORDER BY booking_date DESC,queue_number DESC LIMIT 30""", u["id"])
    return {"profile": dict(d), "stats": dict(s), "recent": [dict(x) for x in rows]}


@app.put("/api/doctor/profile")
async def d_profile_update(request: Request, u=Depends(auth("doctor"))):
    """تحديث جزئي: بيانات العرض (الاسم/التخصص/النبذة/الصورة/المدة) و/أو بيانات الدخول (اسم المستخدم/كلمة المرور).
    تغيير بيانات الدخول يتطلب كلمة المرور الحالية."""
    b = await body(request)
    g = lambda k: str(b.get(k) or "").strip()
    sets, args = [], [u["id"]]

    def add(col, val):  # أسماء الأعمدة ثابتة داخل الكود فقط، والقيم تمر كمعاملات
        args.append(val)
        sets.append(f"{col}=${len(args)}")

    if "name" in b:
        name, spec, desc, img = g("name"), g("specialty"), g("description")[:300], g("profile_image")
        try:
            avg = int(b.get("average_consultation_time"))
        except Exception:
            avg = 0
        if not 2 <= len(name) <= 80: raise Fail(400, "الاسم غير صالح")
        if not spec or len(spec) > 80: raise Fail(400, "التخصص مطلوب")
        if not 1 <= avg <= 240: raise Fail(400, "متوسط الكشف بين 1 و240 دقيقة")
        if img and (not re.match(r"(https?://|data:image/)", img) or len(img) > 800000): raise Fail(400, "الصورة غير صالحة")
        add("name", name); add("specialty", spec); add("description", desc); add("average_consultation_time", avg)
        add("profile_image", img or None)

    if "username" in b or "password" in b:
        row = await pool.fetchrow("SELECT username,password_hash FROM doctors WHERE id=$1", u["id"])
        new_user, new_pw = g("username").lower(), str(b.get("password") or "")
        change_user = bool(new_user) and new_user != row["username"]
        if change_user or new_pw:
            key = f"{u['id']}:profile-pw"
            n, at = tries.get(key, (0, time.time()))
            if time.time() - at > 900:
                n, at = 0, time.time()
            if n >= 10: raise Fail(429, "محاولات كثيرة، حاول بعد 15 دقيقة")
            ok = await asyncio.to_thread(bcrypt.checkpw, str(b.get("current_password") or "").encode()[:72], row["password_hash"].encode())
            if not ok:
                tries[key] = (n + 1, at)
                raise Fail(403, "كلمة المرور الحالية غير صحيحة")
            tries.pop(key, None)
            if change_user:
                if not re.fullmatch(r"[a-z0-9_.]{3,30}", new_user): raise Fail(400, "اسم المستخدم: 3-30 حرفاً إنجليزياً أو أرقاماً")
                add("username", new_user)
            if new_pw:
                if len(new_pw) < 6: raise Fail(400, "كلمة المرور 6 أحرف على الأقل")
                add("password_hash", await asyncio.to_thread(hashpw, new_pw))

    if not sets:
        return {"ok": True}
    try:
        await pool.execute(f"UPDATE doctors SET {', '.join(sets)} WHERE id=$1", *args)
    except asyncpg.UniqueViolationError:
        raise Fail(409, "اسم المستخدم مستخدم بالفعل")
    await changed()
    return {"ok": True}


# ---------- admin ----------
def vdoc(b, create):
    g = lambda k: str(b.get(k) or "").strip()
    o = {"name": g("name"), "specialty": g("specialty"), "description": g("description")[:300], "image": g("profile_image"),
         "username": g("username").lower(), "password": str(b.get("password") or ""),
         "active": b.get("is_active") is not False, "accepting": b.get("accepting") is not False}
    try:
        o["avg"] = int(b.get("average_consultation_time"))
    except Exception:
        o["avg"] = 0
    if not 2 <= len(o["name"]) <= 80: raise Fail(400, "اسم الطبيب غير صالح")
    if not o["specialty"] or len(o["specialty"]) > 80: raise Fail(400, "التخصص مطلوب")
    if not re.fullmatch(r"[a-z0-9_.]{3,30}", o["username"]): raise Fail(400, "اسم المستخدم: 3-30 حرفاً إنجليزياً أو أرقاماً")
    if not 1 <= o["avg"] <= 240: raise Fail(400, "متوسط الكشف بين 1 و240 دقيقة")
    if o["image"] and (not re.match(r"(https?://|data:image/)", o["image"]) or len(o["image"]) > 800000): raise Fail(400, "الصورة غير صالحة")
    if (create or o["password"]) and len(o["password"]) < 6: raise Fail(400, "كلمة المرور 6 أحرف على الأقل")
    return o


def hashpw(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(10)).decode()


@app.get("/api/admin/doctors")
async def a_list(u=Depends(auth("admin"))):
    rows = await pool.fetch(STATS + " ORDER BY d.id")
    qs = await pool.fetch("SELECT doctor_id,queue_number,patient_name,status FROM bookings WHERE booking_date=CURRENT_DATE AND status IN ('Waiting','In Progress') ORDER BY queue_number")
    return {"doctors": [{**adm(r), "queue": [dict(x) for x in qs if x["doctor_id"] == r["id"]]} for r in rows]}


@app.post("/api/admin/doctors", status_code=201)
async def a_create(request: Request, u=Depends(auth("admin"))):
    o = vdoc(await body(request), True)
    try:
        await pool.execute("""INSERT INTO doctors(name,specialty,description,profile_image,username,password_hash,average_consultation_time,is_active,is_accepting_bookings)
          VALUES($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9)""", o["name"], o["specialty"], o["description"], o["image"], o["username"],
                           await asyncio.to_thread(hashpw, o["password"]), o["avg"], o["active"], o["accepting"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "اسم المستخدم مستخدم بالفعل")
    await changed()
    return {"ok": True}


@app.put("/api/admin/doctors/{did}")
async def a_update(did: int, request: Request, u=Depends(auth("admin"))):
    o = vdoc(await body(request), False)
    h = await asyncio.to_thread(hashpw, o["password"]) if o["password"] else None
    try:
        r = await pool.fetchval("""UPDATE doctors SET name=$2,specialty=$3,description=$4,profile_image=NULLIF($5,''),username=$6,
          password_hash=COALESCE($7,password_hash),average_consultation_time=$8,is_active=$9,is_accepting_bookings=$10 WHERE id=$1 RETURNING id""",
                                did, o["name"], o["specialty"], o["description"], o["image"], o["username"], h, o["avg"], o["active"], o["accepting"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "اسم المستخدم مستخدم بالفعل")
    if not r:
        raise Fail(404, "الطبيب غير موجود")
    await changed()
    return {"ok": True}


@app.patch("/api/admin/doctors/{did}")
async def a_patch(did: int, request: Request, u=Depends(auth("admin"))):
    b = await body(request)
    await pool.execute("UPDATE doctors SET is_active=$2 WHERE id=$1", did, b.get("is_active") is True)
    await changed()
    return {"ok": True}


@app.delete("/api/admin/doctors/{did}")
async def a_delete(did: int, u=Depends(auth("admin"))):
    if not await pool.fetchval("DELETE FROM doctors WHERE id=$1 RETURNING id", did):
        raise Fail(404, "الطبيب غير موجود")
    await changed()
    return {"ok": True}


# ---------- موظف الاستقبال: إدارة الطابور الموحّد (أونلاين + حضور مباشر) ----------
# الطبيب يُقرأ من القاعدة (receptionists.doctor_id) في كل طلب ولا يُقبل أبداً من العميل، وكل استعلام مقيّد به؛
# فلا يستطيع الموظف رؤية أو تعديل حجز طبيب آخر حتى لو عرف رقمه. وكل عملية تُسجَّل في queue_audit.
def toint(v):
    try:
        return int(v)
    except Exception:
        return None


async def rec_ctx(u=Depends(auth("receptionist"))):
    r = await pool.fetchrow("SELECT id,name,doctor_id FROM receptionists WHERE id=$1", u["id"])
    if not r:
        raise Fail(401, "الحساب غير موجود أو معطّل")
    return dict(r)


async def r_state(rec, **extra):
    q = await dq(rec["doctor_id"])
    q["receptionist"] = {"name": rec["name"]}
    return {**q, **extra}


async def audit(c, rec, bid, action, **details):
    await c.execute("INSERT INTO queue_audit(doctor_id,receptionist_id,receptionist_name,booking_id,action,details) VALUES($1,$2,$3,$4,$5,$6::jsonb)",
                    rec["doctor_id"], rec["id"], rec["name"], bid, action, json.dumps(details, ensure_ascii=False))


async def lock_get(c, rec, bid, states):
    """يقفل صف الطبيب (يسلسل كل عمليات طابوره) ثم يجلب الحجز بشرط أنه لنفس الطبيب ولليوم الحالي."""
    await c.fetchval("SELECT 1 FROM doctors WHERE id=$1 FOR UPDATE", rec["doctor_id"])
    b = await c.fetchrow("SELECT id,queue_number,patient_name,status FROM bookings WHERE id=$1 AND doctor_id=$2 AND booking_date=CURRENT_DATE", bid, rec["doctor_id"])
    if not b:
        raise Fail(404, "الحجز غير موجود في طابور طبيبك اليوم")
    if b["status"] not in states:
        raise Fail(409, "لا يمكن تنفيذ هذا الإجراء على حالة الحجز الحالية")
    return b


@app.get("/api/reception/queue")
async def r_queue(rec=Depends(rec_ctx)):
    await cleanup_if_due()
    return await r_state(rec)


@app.get("/api/reception/patients")
async def r_search(q: str = "", rec=Depends(rec_ctx)):
    """بحث عن مريض مسجّل (حساب أونلاين أو حضور سابق) لتجنب التكرار. يرجع الحد الأدنى من البيانات فقط (لا بيانات طبية)."""
    q = q.strip()
    if not 3 <= len(q) <= 40:
        raise Fail(400, "اكتب 3 أحرف أو أرقام على الأقل للبحث")
    esc_like = lambda t: re.sub(r"([\\%_])", r"\\\1", t)
    digits = re.sub(r"[\s-]", "", q)
    name_like = "%" + esc_like(q) + "%"
    phone_like = "%" + esc_like(digits) + "%" if re.fullmatch(r"\+?\d+", digits) else name_like
    act = "b.doctor_id=$3 AND b.booking_date=CURRENT_DATE AND b.status IN ('Waiting','In Progress')"
    rows = await pool.fetch(f"""(SELECT 'account' AS kind,p.id,p.name,p.phone,p.age,
        EXISTS(SELECT 1 FROM bookings b WHERE {act} AND b.patient_id=p.id AND b.member_id IS NULL) AS booked
        FROM patients p WHERE p.name ILIKE $1 OR p.phone LIKE $2)
      UNION ALL
      (SELECT 'walkin',w.id,w.name,w.phone,w.age,
        EXISTS(SELECT 1 FROM bookings b WHERE {act} AND b.walkin_id=w.id) AS booked
        FROM walkin_patients w WHERE w.name ILIKE $1 OR w.phone LIKE $2)
      ORDER BY name LIMIT 10""", name_like, phone_like, rec["doctor_id"])
    return [dict(r) for r in rows]


@app.post("/api/reception/bookings", status_code=201)
async def r_book(request: Request, rec=Depends(rec_ctx)):
    """تسجيل مريض حضور مباشر وإدخاله في نفس الطابور الموحّد (المصدر = Walk-in).
    kind: account (مريض له حساب) | walkin (سجل حضور سابق) | new (مريض جديد: name,phone,age)"""
    b, did = await body(request), rec["doctor_id"]
    kind, pid, wid = b.get("kind"), None, None
    if kind == "new":
        name, phone, age = str(b.get("name") or "").strip(), re.sub(r"[\s-]", "", str(b.get("phone") or "")), toint(b.get("age"))
        if not 2 <= len(name) <= 60: raise Fail(400, "اكتب اسم المريض (حرفان على الأقل)")
        if not re.fullmatch(r"\+?\d{8,15}", phone): raise Fail(400, "رقم الهاتف غير صحيح")
        if age is None or not 0 <= age <= 120: raise Fail(400, "العمر غير صحيح")
    elif kind in ("account", "walkin") and toint(b.get("id")):
        pass
    else:
        raise Fail(400, "بيانات المريض غير مكتملة")
    async with pool.acquire() as c, c.transaction():
        await c.fetchval("SELECT 1 FROM doctors WHERE id=$1 FOR UPDATE", did)  # نفس قفل الحجز الأونلاين: لا يتكرر رقم الدور
        if kind == "account":
            p = await c.fetchrow("SELECT id,name,phone FROM patients WHERE id=$1", toint(b.get("id")))
            if not p: raise Fail(404, "المريض غير موجود")
            pid, name, phone = p["id"], p["name"], p["phone"]
        elif kind == "walkin":
            w = await c.fetchrow("SELECT id,name,phone FROM walkin_patients WHERE id=$1", toint(b.get("id")))
            if not w: raise Fail(404, "المريض غير موجود")
            wid, name, phone = w["id"], w["name"], w["phone"]
        else:
            if await c.fetchval("SELECT 1 FROM patients WHERE phone=$1 AND lower(name)=lower($2)", phone, name):
                raise Fail(409, "هذا المريض مسجّل بحساب أونلاين، اختره من نتائج البحث بدل تسجيله من جديد")
            wid = await c.fetchval("SELECT id FROM walkin_patients WHERE phone=$1 AND lower(name)=lower($2) ORDER BY id LIMIT 1", phone, name)
            if not wid:
                wid = await c.fetchval("INSERT INTO walkin_patients(name,phone,age,created_by) VALUES($1,$2,$3,$4) RETURNING id", name, phone, age, rec["id"])
        if await c.fetchval("""SELECT 1 FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status IN ('Waiting','In Progress')
                AND (($2::int IS NOT NULL AND patient_id=$2 AND member_id IS NULL) OR ($3::int IS NOT NULL AND walkin_id=$3))""", did, pid, wid):
            raise Fail(409, "لدى هذا المريض حجز نشط عند الطبيب اليوم")
        row = await c.fetchrow("""INSERT INTO bookings(doctor_id,patient_name,patient_phone,patient_id,walkin_id,queue_number,source,created_by_receptionist)
          SELECT $1::int,$2::text,$3::text,$4::int,$5::int,COALESCE(MAX(queue_number),0)+1,'Walk-in',$6::int FROM bookings
          WHERE doctor_id=$1::int AND booking_date=CURRENT_DATE RETURNING id,queue_number""", did, name, phone, pid, wid, rec["id"])
        await audit(c, rec, row["id"], "add", patient=name, queue=row["queue_number"])
    return await r_state(rec, booking={"id": row["id"], "queue_number": row["queue_number"]})


@app.post("/api/reception/call-next")
async def r_call(rec=Depends(rec_ctx)):
    """استدعاء المريض التالي (أول منتظر بالترتيب): يظهر له في تطبيقه «تم استدعاؤك». الحالة تبقى Waiting حتى بدء الكشف."""
    did = rec["doctor_id"]
    async with pool.acquire() as c, c.transaction():
        await c.fetchval("SELECT 1 FROM doctors WHERE id=$1 FOR UPDATE", did)
        n = await c.fetchrow("SELECT id,queue_number,patient_name FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='Waiting' ORDER BY queue_number LIMIT 1", did)
        if not n:
            raise Fail(409, "لا يوجد مرضى في الانتظار")
        await c.execute("""UPDATE bookings SET called_at=CASE WHEN id=$2 THEN now() END WHERE doctor_id=$1 AND booking_date=CURRENT_DATE
          AND status='Waiting' AND (id=$2 OR called_at IS NOT NULL)""", did, n["id"])
        await audit(c, rec, n["id"], "call", patient=n["patient_name"], queue=n["queue_number"])
    return await r_state(rec)


@app.post("/api/reception/bookings/{bid}/start")
async def r_start(bid: int, rec=Depends(rec_ctx)):
    async with pool.acquire() as c, c.transaction():
        b = await lock_get(c, rec, bid, ("Waiting",))
        if await c.fetchval("SELECT 1 FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='In Progress'", rec["doctor_id"]):
            raise Fail(409, "أنهِ الكشف الحالي أولاً")
        await c.execute("UPDATE bookings SET status='In Progress',started_at=now(),called_at=COALESCE(called_at,now()) WHERE id=$1", bid)
        await audit(c, rec, bid, "start", patient=b["patient_name"], queue=b["queue_number"])
    return await r_state(rec)


@app.post("/api/reception/bookings/{bid}/complete")
async def r_complete(bid: int, rec=Depends(rec_ctx)):
    # لا نشترط وجود تقرير: الطبيب يكتبه/يعدّله من لوحته (وتظهر حالة التقرير للموظف قبل الإنهاء)
    async with pool.acquire() as c, c.transaction():
        b = await lock_get(c, rec, bid, ("In Progress",))
        has = await c.fetchval("SELECT EXISTS(SELECT 1 FROM visit_reports WHERE booking_id=$1)", bid)
        await c.execute("UPDATE bookings SET status='Completed',completed_at=now() WHERE id=$1", bid)
        await audit(c, rec, bid, "complete", patient=b["patient_name"], queue=b["queue_number"], has_report=has)
    return await r_state(rec)


@app.post("/api/reception/bookings/{bid}/skip")
async def r_skip(bid: int, rec=Depends(rec_ctx)):
    """تخطي مريض (لم يحضر/غير جاهز): يعود لآخر الطابور برقم جديد وتُسجَّل المرة، ويمكن استدعاؤه لاحقاً."""
    did = rec["doctor_id"]
    async with pool.acquire() as c, c.transaction():
        b = await lock_get(c, rec, bid, ("Waiting", "In Progress"))
        if b["status"] == "Waiting" and not await c.fetchval("SELECT 1 FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='Waiting' AND id<>$2", did, bid):
            raise Fail(409, "لا يوجد مرضى آخرون في الانتظار")
        mx = await c.fetchval("SELECT MAX(queue_number) FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE", did)
        await c.execute("UPDATE bookings SET status='Waiting',queue_number=$2,started_at=NULL,called_at=NULL,skipped_count=skipped_count+1 WHERE id=$1", bid, mx + 1)
        await audit(c, rec, bid, "skip", patient=b["patient_name"], queue=b["queue_number"], new_queue=mx + 1)
    return await r_state(rec)


@app.post("/api/reception/bookings/{bid}/cancel")
async def r_cancel(bid: int, request: Request, rec=Depends(rec_ctx)):
    reason = str((await body(request)).get("reason") or "").strip()[:200]
    async with pool.acquire() as c, c.transaction():
        b = await lock_get(c, rec, bid, ("Waiting", "In Progress"))
        await c.execute("UPDATE bookings SET status='Cancelled' WHERE id=$1", bid)
        await audit(c, rec, bid, "cancel", patient=b["patient_name"], queue=b["queue_number"], reason=reason)
    return await r_state(rec)


@app.post("/api/reception/bookings/{bid}/move")
async def r_move(bid: int, request: Request, rec=Depends(rec_ctx)):
    """تعديل الترتيب: نقل منتظر إلى موضع (1 = أول الطابور). نعيد توزيع نفس مجموعة أرقام المنتظرين بالترتيب الجديد،
    فلا يتعارض مع أرقام غيرهم، والقيد DEFERRABLE يسمح بالتبديل داخل المعاملة."""
    pos = toint((await body(request)).get("position"))
    did = rec["doctor_id"]
    async with pool.acquire() as c, c.transaction():
        b = await lock_get(c, rec, bid, ("Waiting",))
        rows = await c.fetch("SELECT id,queue_number FROM bookings WHERE doctor_id=$1 AND booking_date=CURRENT_DATE AND status='Waiting' ORDER BY queue_number", did)
        ids, nums = [r["id"] for r in rows], [r["queue_number"] for r in rows]
        if pos is None or not 1 <= pos <= len(ids):
            raise Fail(400, "الموضع غير صحيح")
        old = ids.index(bid) + 1
        if old != pos:
            ids.remove(bid)
            ids.insert(pos - 1, bid)
            await c.execute("SET CONSTRAINTS uq_queue_number DEFERRED")
            await c.execute("UPDATE bookings x SET queue_number=v.n FROM unnest($1::int[],$2::int[]) AS v(id,n) WHERE x.id=v.id", ids, nums)
            await audit(c, rec, bid, "move", patient=b["patient_name"], queue=b["queue_number"], **{"from": old, "to": pos})
    return await r_state(rec)


# ---------- admin: موظفو الاستقبال وسجل العمليات ----------
def vrec(b, create):
    g = lambda k: str(b.get(k) or "").strip()
    o = {"name": g("name"), "email": g("email").lower(), "phone": re.sub(r"[\s-]", "", g("phone")),
         "password": str(b.get("password") or ""), "doctor_id": toint(b.get("doctor_id")), "active": b.get("is_active") is not False}
    if not 2 <= len(o["name"]) <= 60: raise Fail(400, "اسم الموظف غير صالح")
    if len(o["email"]) > 80 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", o["email"]): raise Fail(400, "البريد الإلكتروني غير صالح")
    if not re.fullmatch(r"\+?\d{8,15}", o["phone"]): raise Fail(400, "رقم الهاتف غير صحيح")
    if not o["doctor_id"]: raise Fail(400, "اختر الطبيب المرتبط بالموظف")
    if (create or o["password"]) and len(o["password"]) < 8: raise Fail(400, "كلمة المرور 8 أحرف على الأقل")
    return o


RECS = """SELECT r.id,r.name,r.email,r.phone,r.is_active,r.doctor_id,d.name AS doctor_name,d.specialty,r.created_at
  FROM receptionists r JOIN doctors d ON d.id=r.doctor_id"""


@app.get("/api/admin/receptionists")
async def a_rlist(u=Depends(auth("admin"))):
    return {"receptionists": [dict(r) for r in await pool.fetch(RECS + " ORDER BY r.id")]}


@app.post("/api/admin/receptionists", status_code=201)
async def a_rcreate(request: Request, u=Depends(auth("admin"))):
    o = vrec(await body(request), True)
    if not await pool.fetchval("SELECT 1 FROM doctors WHERE id=$1", o["doctor_id"]):
        raise Fail(404, "الطبيب غير موجود")
    try:
        await pool.execute("INSERT INTO receptionists(doctor_id,name,email,phone,password_hash,is_active,created_by) VALUES($1,$2,$3,$4,$5,$6,$7)",
                           o["doctor_id"], o["name"], o["email"], o["phone"], await asyncio.to_thread(hashpw, o["password"]), o["active"], u["id"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "البريد الإلكتروني مستخدم بالفعل")
    return {"ok": True}


@app.put("/api/admin/receptionists/{rid}")
async def a_rupdate(rid: int, request: Request, u=Depends(auth("admin"))):
    o = vrec(await body(request), False)
    if not await pool.fetchval("SELECT 1 FROM doctors WHERE id=$1", o["doctor_id"]):
        raise Fail(404, "الطبيب غير موجود")
    h = await asyncio.to_thread(hashpw, o["password"]) if o["password"] else None
    try:
        r = await pool.fetchval("""UPDATE receptionists SET doctor_id=$2,name=$3,email=$4,phone=$5,password_hash=COALESCE($6,password_hash),is_active=$7
          WHERE id=$1 RETURNING id""", rid, o["doctor_id"], o["name"], o["email"], o["phone"], h, o["active"])
    except asyncpg.UniqueViolationError:
        raise Fail(409, "البريد الإلكتروني مستخدم بالفعل")
    if not r:
        raise Fail(404, "الموظف غير موجود")
    return {"ok": True}


@app.patch("/api/admin/receptionists/{rid}")
async def a_rpatch(rid: int, request: Request, u=Depends(auth("admin"))):
    b = await body(request)
    if not await pool.fetchval("UPDATE receptionists SET is_active=$2 WHERE id=$1 RETURNING id", rid, b.get("is_active") is True):
        raise Fail(404, "الموظف غير موجود")
    return {"ok": True}


@app.delete("/api/admin/receptionists/{rid}")
async def a_rdelete(rid: int, u=Depends(auth("admin"))):
    if not await pool.fetchval("DELETE FROM receptionists WHERE id=$1 RETURNING id", rid):
        raise Fail(404, "الموظف غير موجود")
    return {"ok": True}


@app.get("/api/admin/audit")
async def a_audit(doctor_id: int = 0, u=Depends(auth("admin"))):
    rows = await pool.fetch("""SELECT a.id,a.action,a.details,a.created_at,a.receptionist_name,d.name AS doctor_name FROM queue_audit a
      JOIN doctors d ON d.id=a.doctor_id WHERE ($1=0 OR a.doctor_id=$1) ORDER BY a.id DESC LIMIT 200""", doctor_id)
    return {"audit": [{**dict(r), "details": jl(r["details"])} for r in rows]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

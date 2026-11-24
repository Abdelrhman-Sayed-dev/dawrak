"""اختبار منطق الأحداث في push.py بقاعدة بيانات وهمية في الذاكرة (يتحقق من: من يستقبل ماذا، ومنع التكرار، وإعادة التنبيه بعد التأخير).
لا يتحقق من صياغة SQL (يحتاج PostgreSQL حقيقي)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import push, webpush

class Rec(dict):
    def __getitem__(s, k): return dict.__getitem__(s, k)
class FakePool:
    def __init__(s):
        s.subs = {}            # id -> dict(role,user_id,endpoint)
        s.guest_links = set()  # (sub_id, booking_id)
        s.events = set()       # (booking_id, kind)
        s.bookings = {}        # id -> dict
        s.receptionists = {10: 1}   # id -> doctor_id
        s.deleted = []
    def waiting(s, did): return sorted([b for b in s.bookings.values() if b["doctor_id"] == did and b["status"] == "Waiting"], key=lambda b: b["queue_number"])
    def ahead(s, b): return len([x for x in s.waiting(b["doctor_id"]) if x["queue_number"] < b["queue_number"]])
    async def execute(s, q, *a):
        if "DELETE FROM push_events" in q:
            kind = "near" if "kind='near'" in q else "next"
            for i in a[0]: s.events.discard((i, kind))
        elif "DELETE FROM push_subscriptions WHERE id=ANY" in q: s.deleted += a[0]
        elif "DELETE FROM push_guest_bookings WHERE subscription_id" in q: s.guest_links = {l for l in s.guest_links if l[0] != a[0]}
        elif "UPDATE push_subscriptions SET p256dh" in q or "last_ok_at" in q or "fail_count=fail_count+1" in q or "NOT IN (SELECT id" in q: pass
        else: raise AssertionError("unexpected execute: " + q[:80])
    async def fetchval(s, q, *a):
        if "INSERT INTO push_events" in q:
            k = (a[0], a[1])
            if k in s.events: return None
            s.events.add(k); return 1
        if "INSERT INTO push_guest_bookings" in q:
            if a in s.guest_links: return None
            s.guest_links.add(a); return 1
        if "SELECT name FROM doctors" in q: return "د. أحمد"
        if "INSERT INTO push_subscriptions" in q:
            ep, p, au, role, uid, ua = a
            sid = next((i for i, v in s.subs.items() if v["endpoint"] == ep), len(s.subs) + 1)
            s.subs[sid] = dict(id=sid, endpoint=ep, p256dh=p, auth=au, role=role, user_id=uid); return sid
        if "SELECT id FROM push_subscriptions WHERE endpoint" in q: return next((i for i, v in s.subs.items() if v["endpoint"] == a[0] and v["role"] == "guest"), None)
        raise AssertionError("unexpected fetchval: " + q[:80])
    async def fetchrow(s, q, *a):
        b = s.bookings.get(a[0])
        return Rec(b, dname="د. أحمد", ahead=s.ahead(b)) if b else None
    async def fetch(s, q, *a):
        if "WITH q AS" in q:
            return [Rec(id=b["id"], patient_id=b["patient_id"], queue_number=b["queue_number"], ahead=s.ahead(b),
                        n_near=(b["id"], "near") in s.events, n_next=(b["id"], "next") in s.events) for b in s.waiting(a[0])]
        if "s.role=$1 AND s.user_id=ANY" in q: return [Rec(v) for v in s.subs.values() if v["role"] == a[0] and v["user_id"] in a[1]]
        if "push_guest_bookings g WHERE g.booking_id" in q:
            bid, pid = a
            return [Rec(v) for v in s.subs.values() if (v["role"] == "patient" and pid and v["user_id"] == pid) or (v["id"], bid) in s.guest_links]
        if "FROM receptionists r WHERE" in q:
            did, rec = a
            return [Rec(v) for v in s.subs.values() if (v["role"] == "doctor" and v["user_id"] == did) or (rec and v["role"] == "receptionist" and s.receptionists.get(v["user_id"]) == did)]
        raise AssertionError("unexpected fetch: " + q[:80])

sent = []
def fake_send(vapid, ep, p, a, payload, **k): sent.append((ep, payload)); return 201, ""
webpush.send = fake_send
class V: public = "x"

def mk():
    pool = FakePool(); P = push.Push(pool, V(), near=2)
    for i, st in enumerate(["Waiting"] * 5, 1):
        pool.bookings[i] = dict(id=i, doctor_id=1, patient_id=None, patient_name=f"م{i}", queue_number=i, source="Online", status=st)
    pool.subs[1] = dict(id=1, endpoint="https://fcm.googleapis.com/g1", p256dh="p", auth="a", role="guest", user_id=None)   # ضيف لحجز 4
    pool.guest_links.add((1, 4))
    pool.subs[2] = dict(id=2, endpoint="https://fcm.googleapis.com/doc", p256dh="p", auth="a", role="doctor", user_id=1)
    pool.subs[3] = dict(id=3, endpoint="https://fcm.googleapis.com/rec", p256dh="p", auth="a", role="receptionist", user_id=10)
    pool.subs[4] = dict(id=4, endpoint="https://fcm.googleapis.com/pat", p256dh="p", auth="a", role="patient", user_id=77)
    pool.bookings[5]["patient_id"] = 77
    return pool, P

def run(c): return asyncio.run(c)
def titles(): return [(e.rsplit("/", 1)[1], p["title"], p["url"]) for e, p in sent]

def test_queue_near_next_once_and_reset():
    pool, P = mk(); sent.clear()
    run(P.ev_queue(1)); assert titles() == [], titles()              # حجز 4 و5 ليس قريباً (أمامهما 3 و4)
    pool.bookings[1]["status"] = "In Progress"; run(P.ev_queue(1))   # الحجز 4 أصبح أمامه 2 (حجزا 2 و3)
    t = titles(); assert t == [("g1", "اقترب دورك", "/track")], t
    assert "أمامك مريضان" in sent[0][1]["body"] and "دورك رقم 4" in sent[0][1]["body"], sent[0]
    sent.clear(); run(P.ev_queue(1)); assert titles() == [], "لا تكرار"
    pool.bookings[2]["status"] = "In Progress"; pool.bookings[1]["status"] = "Completed"; sent.clear(); run(P.ev_queue(1)); assert titles() == [("pat", "اقترب دورك", "/patient/dashboard")], titles()   # ضيف 4 سبق تنبيهه، والمريض صاحب الحساب (حجز 5) صار أمامه 2
    pool.bookings[3]["status"] = "Cancelled"; pool.bookings[4]["queue_number"] = 4; sent.clear()
    run(P.ev_queue(1)); t = titles(); assert ("g1", "أنت التالي في الدور", "/track") in t, t
    sent.clear(); run(P.ev_queue(1)); assert titles() == []
    # تأجيل: يرجع للخلف ثم يعود قريباً => يحق تنبيهه ثانية
    pool.bookings[4]["queue_number"] = 99; pool.bookings[6] = dict(id=6, doctor_id=1, patient_id=None, patient_name="n", queue_number=10, source="Online", status="Waiting")
    for i in (7, 8, 9, 10): pool.bookings[i] = dict(id=i, doctor_id=1, patient_id=None, patient_name="n", queue_number=10 + i, source="Online", status="Waiting")
    run(P.ev_queue(1)); assert (4, "near") not in pool.events and (4, "next") not in pool.events
    for i in (6, 7, 8, 9, 10): pool.bookings[i]["status"] = "Cancelled"
    sent.clear(); run(P.ev_queue(1)); assert any(x[1] in ("أنت التالي في الدور", "اقترب دورك") for x in titles()), titles()

def test_patient_account_gets_account_url_and_staff_split():
    pool, P = mk(); sent.clear()
    run(P.ev_booked(5)); t = titles()
    assert ("pat", "تم تأكيد حجزك", "/patient/dashboard") in t and ("doc", "حجز جديد", "/doctor/dashboard") in t and ("rec", "حجز جديد", "/receptionist/dashboard") in t, t
    assert not any(x[0] == "g1" for x in t)            # ضيف حجز آخر لا يستلم شيئاً
    pool.bookings[5]["source"] = "Walk-in"; sent.clear(); run(P.ev_booked(5)); t = titles()
    assert not any(x[0] == "rec" for x in t) and ("doc", "تسجيل حضور مباشر", "/doctor/dashboard") in t, t

def test_started_cancel_report_postpone_routing():
    pool, P = mk(); sent.clear()
    pool.bookings[4]["status"] = "In Progress"; run(P.ev_started(4)); t = titles()
    assert ("g1", "جاء دورك الآن", "/track") in t and ("doc", "بدأ كشف مريض", "/doctor/dashboard") in t and not any(x[0] == "rec" for x in t), t
    sent.clear(); run(P.ev_cancelled(5, "staff")); assert ("pat", "تم إلغاء حجزك", "/patient/dashboard") in titles()
    sent.clear(); run(P.ev_cancelled(5, "patient")); t = titles(); assert {x[0] for x in t} >= {"doc", "rec"} and not any(x[0] == "pat" for x in t), t
    sent.clear(); run(P.ev_report(5)); assert titles() == [("pat", "تم إصدار الروشتة", "/patient/dashboard")]
    sent.clear(); run(P.ev_report(4)); assert titles() == [], "الضيف لا يرى التقارير"
    sent.clear(); run(P.ev_postponed(5)); assert {x[0] for x in titles()} >= {"doc", "rec"}

def test_guest_subscribe_links_and_confirms_once():
    pool, P = mk(); sent.clear()
    run(P.subscribe_guest("https://fcm.googleapis.com/new", "p", "a", "ua", [3, 4])); t = titles()
    assert [x[1] for x in t].count("تم تأكيد حجزك") == 2, t
    sent.clear(); run(P.subscribe_guest("https://fcm.googleapis.com/new", "p", "a", "ua", [3, 4])); assert titles() == [], "إعادة المزامنة لا ترسل تأكيداً ثانياً"
    sent.clear(); run(P.subscribe_guest("https://fcm.googleapis.com/new", "p", "a", "ua", [3], welcome=True)); assert titles() == [("new", "تم تفعيل الإشعارات", "/track")]

def test_expired_subscription_deleted_and_disabled_is_noop():
    pool, P = mk(); sent.clear()
    webpush.send = lambda *a, **k: (410, "gone")
    run(P.ev_started(4)); assert 1 in pool.deleted and 2 in pool.deleted
    webpush.send = fake_send
    off = push.Push(pool, None)
    async def go():
        await off.fire(off.ev_booked(5))
    sent.clear(); run(go()); assert sent == []

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("PASS", n)

"""منطق إشعارات الموبايل (Web Push) لـ«دورك».

الفكرة: كل حدث في الطابور (حجز / إلغاء / تأجيل / بدء كشف / روشتة / تغيير ترتيب) يستدعي دالة هنا بعد نجاح الـ commit.
لا يوجد أي تعديل على منطق الحجز أو الطابور: هذه الوحدة تقرأ فقط من جداول الحجوزات، وتكتب فقط في جداول push_*.
أي فشل هنا يُسجَّل في اللوج ولا يصل أبداً لمسار الحجز نفسه. وإن لم تُضبط مفاتيح VAPID تتعطل الوحدة بالكامل بلا أي أثر.
"""
import asyncio

import webpush

PUSH_DDL = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  role VARCHAR(12) NOT NULL CHECK (role IN ('patient','doctor','receptionist','guest')),
  user_id INT,
  user_agent VARCHAR(200) NOT NULL DEFAULT '',
  fail_count INT NOT NULL DEFAULT 0,
  last_ok_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (role, user_id);
CREATE TABLE IF NOT EXISTS push_guest_bookings (
  subscription_id BIGINT NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  PRIMARY KEY (subscription_id, booking_id)
);
CREATE INDEX IF NOT EXISTS idx_push_guest_booking ON push_guest_bookings (booking_id);
CREATE TABLE IF NOT EXISTS push_events (
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind VARCHAR(12) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (booking_id, kind)
);
"""

URL_BY_ROLE = {"patient": "/patient/dashboard", "guest": "/track", "doctor": "/doctor/dashboard", "receptionist": "/receptionist/dashboard"}
SUB_COLS = "s.id,s.endpoint,s.p256dh,s.auth,s.role"
MAX_SUBS_PER_USER = 10


def ahead_ar(n: int) -> str:
    """نفس صياغة الواجهة (aheadTxt)."""
    return ("أنت التالي في الدور" if n == 0 else "أمامك مريض واحد" if n == 1 else "أمامك مريضان" if n == 2
            else f"أمامك {n} مرضى" if n <= 10 else f"أمامك {n} مريضاً")


class Push:
    def __init__(self, pool, vapid, near: int = 2, sync: bool = False):
        self.pool, self.vapid, self.near, self.sync = pool, vapid, max(1, min(10, near)), sync
        self._ready, self._lock, self._tasks = False, asyncio.Lock(), set()

    @property
    def enabled(self) -> bool:
        return self.vapid is not None

    @property
    def public_key(self) -> str:
        return self.vapid.public if self.vapid else ""

    async def ensure(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.pool.execute(PUSH_DDL)
                self._ready = True

    async def fire(self, coro):
        """يشغّل حدث إشعار بدون أن يبطئ الطلب أو يكسره أبداً. PUSH_SYNC=1 للبيئات التي تقتل المهام بعد الرد (Serverless)."""
        if not self.enabled:
            coro.close()
            return

        async def run():
            try:
                await self.ensure()
                await coro
            except Exception as e:
                print("push:", repr(e))
        if self.sync:
            await run()
        else:
            t = asyncio.ensure_future(run())
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    # ---------- الإرسال ----------
    async def _deliver(self, subs, payload):
        sem = asyncio.Semaphore(8)

        async def one(s):
            async with sem:
                try:
                    code, _ = await asyncio.to_thread(webpush.send, self.vapid, s["endpoint"], s["p256dh"], s["auth"], payload)
                except Exception as e:
                    print("push send:", repr(e))
                    code = 0
                return s["id"], code
        res = await asyncio.gather(*(one(s) for s in subs))
        gone = [i for i, c in res if c in (404, 410)]
        ok = [i for i, c in res if 200 <= c < 300]
        bad = [i for i, c in res if c not in (404, 410) and not 200 <= c < 300]
        if gone:
            await self.pool.execute("DELETE FROM push_subscriptions WHERE id=ANY($1::bigint[])", gone)
        if ok:
            await self.pool.execute("UPDATE push_subscriptions SET last_ok_at=now(),fail_count=0 WHERE id=ANY($1::bigint[])", ok)
        if bad:
            await self.pool.execute("UPDATE push_subscriptions SET fail_count=fail_count+1 WHERE id=ANY($1::bigint[])", bad)
        return len(ok)

    async def send_note(self, subs, title, body, kind, tag, url=None) -> bool:
        """يرسل نفس الإشعار لكل الأجهزة، والرابط يختلف حسب الدور (مريض/ضيف/طبيب/استقبال). يرجع True لو وُجد جهاز واحد على الأقل."""
        subs = list(subs or [])
        if not self.enabled or not subs:
            return False
        groups = {}
        for s in subs:
            groups.setdefault(s["role"], []).append(s)
        await asyncio.gather(*(self._deliver(v, {"title": title, "body": body, "url": url or URL_BY_ROLE.get(r, "/"), "kind": kind, "tag": tag})
                               for r, v in groups.items()))
        return True

    # ---------- من يستقبل ----------
    async def subs_users(self, role, ids):
        ids = [i for i in ids if i]
        if not ids:
            return []
        return await self.pool.fetch(f"SELECT {SUB_COLS} FROM push_subscriptions s WHERE s.role=$1 AND s.user_id=ANY($2::int[])", role, ids)

    async def subs_booking(self, bid, pid):
        """أجهزة صاحب الحجز: حساب المريض (لو الحجز بحسابه) + أجهزة الضيف المرتبطة بهذا الحجز."""
        return await self.pool.fetch(f"""SELECT {SUB_COLS} FROM push_subscriptions s
          WHERE (s.role='patient' AND $2::int IS NOT NULL AND s.user_id=$2::int)
             OR s.id IN (SELECT g.subscription_id FROM push_guest_bookings g WHERE g.booking_id=$1::int)""", bid, pid)

    async def subs_staff(self, did, receptionists=True):
        return await self.pool.fetch(f"""SELECT {SUB_COLS} FROM push_subscriptions s
          WHERE (s.role='doctor' AND s.user_id=$1::int)
             OR (s.role='receptionist' AND $2::boolean AND s.user_id IN (SELECT r.id FROM receptionists r WHERE r.doctor_id=$1::int AND r.is_active))""", did, receptionists)

    async def _booking(self, bid):
        return await self.pool.fetchrow("""SELECT b.id,b.doctor_id,b.patient_id,b.patient_name,b.queue_number,b.source,b.status,d.name AS dname,
          (SELECT count(*) FROM bookings x WHERE x.doctor_id=b.doctor_id AND x.booking_date=b.booking_date AND x.status='Waiting' AND x.queue_number<b.queue_number)::int AS ahead
          FROM bookings b JOIN doctors d ON d.id=b.doctor_id WHERE b.id=$1""", bid)

    async def _claim(self, bid, *kinds) -> bool:
        """يسجّل أن إشعاراً من هذا النوع أُرسل لهذا الحجز (مرة واحدة فقط). True = أنت أول من سجّله."""
        first = False
        for k in kinds:
            if await self.pool.fetchval("INSERT INTO push_events(booking_id,kind) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", bid, k):
                first = True
        return first

    def confirm_text(self, b):
        n, dn = b["queue_number"], b["dname"]
        return "تم تأكيد حجزك", f"دورك رقم {n} عند {dn}. {ahead_ar(b['ahead'])}" if b["status"] == "Waiting" else f"دورك رقم {n} عند {dn}"

    # ---------- الأحداث ----------
    async def ev_booked(self, bid):
        """حجز جديد (مريض بحساب / ضيف / تسجيل حضور من الاستقبال)."""
        b = await self._booking(bid)
        if not b:
            return
        if b["patient_id"]:
            subs = await self.subs_users("patient", [b["patient_id"]])
            if subs:
                if b["ahead"] <= self.near:   # التأكيد نفسه يذكر الترتيب، فلا نكرر بإشعار «اقترب دورك»
                    await self._claim(bid, "near", *(("next",) if b["ahead"] == 0 else ()))
                t, body = self.confirm_text(b)
                await self.send_note(subs, t, body, "booked", f"q{bid}")
        online = b["source"] == "Online"
        await self.send_note(await self.subs_staff(b["doctor_id"], receptionists=online),
                             "حجز جديد" if online else "تسجيل حضور مباشر", f"{b['patient_name']} — دور رقم {b['queue_number']}", "staff_new", f"b{bid}-new")

    async def ev_started(self, bid):
        b = await self._booking(bid)
        if not b:
            return
        await self.send_note(await self.subs_booking(bid, b["patient_id"]), "جاء دورك الآن", f"توجّه إلى عيادة {b['dname']}", "start", f"q{bid}")
        await self.send_note(await self.subs_staff(b["doctor_id"], receptionists=False), "بدأ كشف مريض", f"{b['patient_name']} — دور رقم {b['queue_number']}", "staff_start", f"b{bid}-start")
        await self.ev_queue(b["doctor_id"])

    async def ev_cancelled(self, bid, by):
        """by = staff (ألغاه الاستقبال: نخبر المريض) | patient (ألغاه المريض: نخبر الطبيب والاستقبال)."""
        b = await self._booking(bid)
        if not b:
            return
        if by == "staff":
            await self.send_note(await self.subs_booking(bid, b["patient_id"]), "تم إلغاء حجزك", f"ألغت العيادة حجزك رقم {b['queue_number']} عند {b['dname']}", "cancel", f"q{bid}")
        else:
            await self.send_note(await self.subs_staff(b["doctor_id"]), "إلغاء حجز", f"{b['patient_name']} ألغى حجزه (دور رقم {b['queue_number']})", "staff_cancel", f"b{bid}-cancel")
        await self.ev_queue(b["doctor_id"])

    async def ev_postponed(self, bid):
        b = await self._booking(bid)
        if not b:
            return
        await self.send_note(await self.subs_staff(b["doctor_id"]), "تأجيل حجز", f"{b['patient_name']} أجّل دوره لآخر الطابور (دور رقم {b['queue_number']})", "staff_post", f"b{bid}-post")
        await self.ev_queue(b["doctor_id"])

    async def ev_report(self, bid):
        """أول مرة يُحفظ فيها تقرير/روشتة الكشف. الضيف لا حساب له فلا يرى التقارير، فلا يصله شيء."""
        b = await self._booking(bid)
        if not b or not b["patient_id"]:
            return
        await self.send_note(await self.subs_users("patient", [b["patient_id"]]), "تم إصدار الروشتة",
                             f"كتب {b['dname']} تقرير الكشف والروشتة. افتح «حجوزاتي» لعرضها", "report", f"r{bid}")

    async def ev_queue(self, did):
        """يُستدعى بعد أي تغيير يقلّل عدد من أمامهم: «اقترب دورك» (أمامك ≤ PUSH_NEAR) و«أنت التالي» (أمامك 0)، مرة واحدة لكل حجز."""
        rows = await self.pool.fetch("""WITH q AS (SELECT id,patient_id,queue_number,(row_number() OVER (ORDER BY queue_number)-1)::int AS ahead
              FROM bookings WHERE doctor_id=$1::int AND booking_date=CURRENT_DATE AND status='Waiting')
            SELECT q.id,q.patient_id,q.queue_number,q.ahead,
              EXISTS(SELECT 1 FROM push_events e WHERE e.booking_id=q.id AND e.kind='near') AS n_near,
              EXISTS(SELECT 1 FROM push_events e WHERE e.booking_id=q.id AND e.kind='next') AS n_next
            FROM q ORDER BY q.queue_number""", did)
        reset_near = [r["id"] for r in rows if r["ahead"] > self.near and r["n_near"]]     # رجع للخلف (تأجيل/نقل): يحق تنبيهه ثانية
        reset_next = [r["id"] for r in rows if r["ahead"] > 0 and r["n_next"]]
        if reset_near:
            await self.pool.execute("DELETE FROM push_events WHERE kind='near' AND booking_id=ANY($1::int[])", reset_near)
        if reset_next:
            await self.pool.execute("DELETE FROM push_events WHERE kind='next' AND booking_id=ANY($1::int[])", reset_next)
        dname = None
        for r in rows:
            a = r["ahead"]
            if a == 0 and not r["n_next"]:
                kind, claim = "next", ("near", "next")
            elif 0 < a <= self.near and not r["n_near"]:
                kind, claim = "near", ("near",)
            else:
                continue
            subs = await self.subs_booking(r["id"], r["patient_id"])
            if subs and await self._claim(r["id"], *claim):
                if dname is None:
                    dname = await self.pool.fetchval("SELECT name FROM doctors WHERE id=$1", did) or ""
                if kind == "next":
                    title, body = "أنت التالي في الدور", f"استعد، دورك رقم {r['queue_number']} هو القادم عند {dname}"
                else:
                    title, body = "اقترب دورك", f"{ahead_ar(a)}، دورك رقم {r['queue_number']} عند {dname}"
                await self.send_note(subs, title, body, kind, f"q{r['id']}")

    # ---------- الاشتراكات ----------
    async def _upsert(self, ep, p, a, role, uid, ua):
        sid = await self.pool.fetchval("""INSERT INTO push_subscriptions(endpoint,p256dh,auth,role,user_id,user_agent) VALUES($1,$2,$3,$4,$5,$6)
          ON CONFLICT (endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh,auth=EXCLUDED.auth,role=EXCLUDED.role,user_id=EXCLUDED.user_id,
            user_agent=EXCLUDED.user_agent,fail_count=0 RETURNING id""", ep, p, a, role, uid, (ua or "")[:200])
        await self.pool.execute("DELETE FROM push_guest_bookings WHERE subscription_id=$1", sid)   # الجهاز يتبع صاحبه الجديد فقط
        return sid

    async def subscribe_account(self, role, uid, ep, p, a, ua, welcome=False):
        await self.ensure()
        sid = await self._upsert(ep, p, a, role, uid, ua)
        await self.pool.execute("""DELETE FROM push_subscriptions WHERE role=$1 AND user_id=$2
          AND id NOT IN (SELECT id FROM push_subscriptions WHERE role=$1 AND user_id=$2 ORDER BY id DESC LIMIT $3)""", role, uid, MAX_SUBS_PER_USER)
        if welcome:
            await self._send_one(sid, ep, p, a, "تم تفعيل الإشعارات", {"patient": "ستصلك إشعارات الحجز والدور والروشتة على هذا الجهاز",
                                 "doctor": "ستصلك إشعارات الحجوزات الجديدة وتحديثات الطابور", "receptionist": "ستصلك إشعارات الحجوزات الجديدة وتحديثات الطابور"}[role], role)
        return sid

    async def subscribe_guest(self, ep, p, a, ua, bids, welcome=False):
        """bids = أرقام حجوزات تم التحقق منها (الهاتف يطابق). يرجع عدد الحجوزات الجديدة المربوطة، ويرسل تأكيد الحجز لكل جديد."""
        await self.ensure()
        existing = await self.pool.fetchval("SELECT id FROM push_subscriptions WHERE endpoint=$1 AND role='guest'", ep)
        sid = existing or await self._upsert(ep, p, a, "guest", None, ua)
        if existing:
            await self.pool.execute("UPDATE push_subscriptions SET p256dh=$2,auth=$3,fail_count=0 WHERE id=$1", sid, p, a)
        new = []
        for bid in bids[:10]:
            if await self.pool.fetchval("INSERT INTO push_guest_bookings(subscription_id,booking_id) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1", sid, bid):
                new.append(bid)
        for bid in new:
            b = await self._booking(bid)
            if b:
                if b["status"] == "Waiting" and b["ahead"] <= self.near:
                    await self._claim(bid, "near", *(("next",) if b["ahead"] == 0 else ()))
                t, body = self.confirm_text(b)
                await self._send_one(sid, ep, p, a, t, body, "guest", f"q{bid}", "booked")
        if welcome and not new:
            await self._send_one(sid, ep, p, a, "تم تفعيل الإشعارات", "ستصلك إشعارات دورك على هذا الجهاز", "guest")
        return len(new)

    async def _send_one(self, sid, ep, p, a, title, body, role, tag="welcome", kind="welcome"):
        if self.enabled:
            await self.send_note([{"id": sid, "endpoint": ep, "p256dh": p, "auth": a, "role": role}], title, body, kind, tag)

    async def unsubscribe(self, ep):
        await self.pool.execute("DELETE FROM push_subscriptions WHERE endpoint=$1", ep)

    # ---------- تذكير الأدوية (يستدعيه مجدول خارجي) ----------
    async def cron_doses(self) -> int:
        """يرسل الجرعات التي حان موعدها فقط لمن لديه جهاز مشترك، بنفس علامة notified_at التي يستخدمها /api/me/reminders/due
        (فلا تتكرر الجرعة بين الإشعار والتنبيه داخل التطبيق). من ليس لديه اشتراك يبقى تنبيهه داخل التطبيق كما كان."""
        if not self.enabled:
            return 0
        await self.ensure()
        rows = await self.pool.fetch("""UPDATE medicine_doses x SET notified_at=now() FROM medicine_reminders r
          WHERE x.reminder_id=r.id AND r.is_active AND x.status='Pending' AND x.notified_at IS NULL
            AND x.scheduled_at<=now() AND x.scheduled_at>now()-interval '2 hours'
            AND EXISTS(SELECT 1 FROM push_subscriptions s WHERE s.role='patient' AND s.user_id=r.patient_id)
          RETURNING x.id,r.patient_id,r.medicine_name,r.dose_note,r.for_name""")
        for r in rows:
            body = r["medicine_name"] + (f" — {r['dose_note']}" if r["dose_note"] else "") + (f" ({r['for_name']})" if r["for_name"] else "")
            await self.send_note(await self.subs_users("patient", [r["patient_id"]]), "حان موعد الدواء", body, "med", f"med{r['id']}", "/patient/reminders")
        return len(rows)

    async def cleanup(self):
        await self.ensure()
        await self.pool.execute("DELETE FROM push_events WHERE created_at < now() - interval '3 days'")
        await self.pool.execute("DELETE FROM push_guest_bookings WHERE booking_id IN (SELECT id FROM bookings WHERE booking_date < CURRENT_DATE)")
        await self.pool.execute("DELETE FROM push_subscriptions WHERE fail_count >= 5 OR (role='guest' AND NOT EXISTS (SELECT 1 FROM push_guest_bookings g WHERE g.subscription_id=push_subscriptions.id) AND created_at < now() - interval '2 days')")

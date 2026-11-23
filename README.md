# دورك (DAWRAK) — نظام حجز دور العيادات

FastAPI + PostgreSQL، والواجهة ملف واحد `index.html` يقدّمه نفس السيرفر.

## روابط الدخول (بوابة منفصلة لكل دور)
| الدور | صفحة الدخول | بعد الدخول | تسجيل جديد |
|---|---|---|---|
| المريض | `/patient/login` | `/patient/dashboard` | `/patient/register` |
| الطبيب | `/doctor/login` | `/doctor/dashboard` | لا يوجد (يضيفه المدير) |
| المدير | `/admin/login` | `/admin/dashboard` | لا يوجد (إنشاء من السيرفر فقط) |

الـ API: `POST /api/patient/login` و`/api/doctor/login` و`/api/admin/login` (الدور يحدده المسار، لا ما يرسله العميل). تم حذف `/api/login` القديم.

## تشغيل محلي
```
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
export JWT_SECRET="نص-عشوائي-طويل"       # لو تُرك فارغاً يُولَّد سر مؤقت (تنتهي الجلسات عند إعادة التشغيل)
psql -d clinic_queue -f database.sql
python create_admin.py <username>        # ينشئ المدير ويطلب كلمة المرور (12+ حرفاً)
python main.py                           # http://localhost:3000
```

## الحسابات
- **المدير**: `python create_admin.py <username>` فقط. لا يوجد تسجيل عام ولا كلمة مرور في الكود أو في `database.sql`.
- **الأطباء**: من لوحة المدير (اسم مستخدم فريد وكلمة مرور مشفّرة bcrypt لكل طبيب).
- **المرضى**: يسجّلون بأنفسهم.

## متغيرات البيئة
| الاسم | الوصف |
|---|---|
| `DATABASE_URL` | رابط PostgreSQL (لـ Neon: بدون `&channel_binding=require`) |
| `JWT_SECRET` | سر توقيع التوكن (إجباري في الإنتاج؛ Render يولّده) |
| `GOOGLE_CLIENT_ID` | اختياري، لتسجيل الدخول بجوجل (للمرضى فقط). لو تُرك فارغاً يختفي زر جوجل |

## فحص سريع للصلاحيات (بعد التشغيل)
```
curl -i localhost:3000/api/admin/doctors                       # 401 بدون توكن
curl -i -XPOST localhost:3000/api/admin/login -H 'content-type: application/json' -d '{"username":"x","password":"y"}'   # 401
# توكن طبيب على /api/admin/doctors => 401 ، توكن مريض على /api/doctor/queue => 401
```

## تسجيل الدخول بجوجل (للمرضى فقط)
1. Google Cloud Console ← Google Auth Platform ← Clients ← Create client ← **Web application**.
2. في **Authorized JavaScript origins** أضف `http://localhost:3000` ورابط الموقع (مثل `https://dawrak.onrender.com`) بدون `/` في آخره. اترك Redirect URIs فارغاً.
3. انسخ الـ Client ID وضعه في المتغير `GOOGLE_CLIENT_ID` (لا حاجة لـ Client Secret).
4. من **Audience**: اضغط Publish app (أو أضف Test users أثناء التجربة).
5. المكتبات `google-auth` و`requests` موجودة في `requirements.txt`.

ملاحظات أمان:
- المريض الجديد بجوجل يكمل بياناته (هاتف/عمر...) ثم يُنشأ حسابه، وكلمة مروره عشوائية (يدخل بجوجل فقط).
- التسجيل اليدوي لا يقبل إيميلاً كاسم مستخدم (حروف/أرقام/_/. فقط)، حتى لا يستطيع أحد حجز إيميل غيره والاستيلاء على حسابه عند دخوله بجوجل.

## بدء وإنهاء الحجز اليومي
- موظف الاستقبال يفتح تابة **الحجز** ويضغط «بدء الحجز لليوم»؛ قبل ذلك يظهر للمرضى «الحجز مغلق حالياً» ولا يقبل السيرفر أي حجز أونلاين.
- «إنهاء الحجز» يوقف الحجز الأونلاين الجديد فقط، والحجوزات الموجودة تبقى في الطابور.
- الحجز يُغلق تلقائياً مع بداية اليوم التالي (عمود `doctors.booking_open_date`)، فيبدأ الموظف كل يوم من جديد.
- تسجيل مرضى الحضور المباشر من الاستقبال متاح دائماً. تم حذف زر الطبيب (إيقاف/فتح الحجوزات)؛ التحكم في الحجز من موظف الاستقبال فقط.
- تم حذف تابة الاستدعاء ومسار `/api/reception/call-next`، وكذلك «إرجاع لآخر الطابور» ومسار `/api/reception/bookings/{id}/skip`. لقاعدة موجودة شغّل `migration_booking_toggle.sql`.

## صفحات الطبيب
| الصفحة | الرابط |
|---|---|
| الرئيسية (المريض الحالي + إحصائيات اليوم) | `/doctor/dashboard` |
| الإحصائيات (داشبورد بالرسوم: حجوزات يومية، حالة الحجوزات، المصدر، ساعات الذروة، التقييمات) | `/doctor/stats` |
| ملفي (بياناتي قابلة للطي + تغيير اسم المستخدم/كلمة المرور) | `/doctor/profile` |

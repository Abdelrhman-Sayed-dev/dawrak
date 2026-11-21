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
| `GOOGLE_CLIENT_ID` | اختياري، لتسجيل الدخول بجوجل (للمرضى فقط) |

## فحص سريع للصلاحيات (بعد التشغيل)
```
curl -i localhost:3000/api/admin/doctors                       # 401 بدون توكن
curl -i -XPOST localhost:3000/api/admin/login -H 'content-type: application/json' -d '{"username":"x","password":"y"}'   # 401
# توكن طبيب على /api/admin/doctors => 401 ، توكن مريض على /api/doctor/queue => 401
```

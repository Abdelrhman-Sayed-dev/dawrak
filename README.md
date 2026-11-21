# دورك (DAWRAK) — نظام حجز دور العيادات

FastAPI + PostgreSQL، والواجهة ملف واحد `index.html` يقدّمه نفس السيرفر.

## تشغيل محلي
```
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."   # قاعدة فيها جداول database.sql
export JWT_SECRET="نص-عشوائي-طويل"
python main.py                           # http://localhost:3000
```

## متغيرات البيئة
| الاسم | الوصف |
|---|---|
| `DATABASE_URL` | رابط PostgreSQL (لـ Neon: بدون `&channel_binding=require`) |
| `JWT_SECRET` | سر توقيع التوكن (إجباري في الإنتاج) |
| `GOOGLE_CLIENT_ID` | اختياري، لتسجيل الدخول بجوجل |

## الحسابات التجريبية (غيّرها فوراً)
`admin / Admin@123` — الأطباء `ahmed` و`sara` و`khaled` / `Doctor@123`

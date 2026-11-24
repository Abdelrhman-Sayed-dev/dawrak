"""يولّد مفاتيح VAPID لإشعارات الموبايل (مرة واحدة فقط لكل موقع).

    python generate_vapid.py

ضع VAPID_PRIVATE_KEY في متغيرات البيئة على Render (سرّي ولا يُرفع لأي مستودع).
المفتاح العام يقرؤه السيرفر تلقائياً من الخاص ويعطيه للمتصفح، فلا حاجة لضبطه يدوياً.
تغيير المفتاح لاحقاً يُبطل اشتراكات الأجهزة الحالية (تعيد الاشتراك تلقائياً عند الفتح التالي).
"""
import webpush

priv, pub = webpush.generate_keys()
print("VAPID_PRIVATE_KEY=" + priv)
print("VAPID_SUBJECT=mailto:your-email@example.com   # غيّره لبريدك")
print("\n(المفتاح العام المشتق، للعلم فقط:", pub + ")")

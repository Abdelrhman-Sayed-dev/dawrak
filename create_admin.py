"""إنشاء/تغيير حساب مدير من جهة السيرفر فقط (لا توجد أي واجهة عامة لإنشاء مدير).

الاستخدام:
    export DATABASE_URL="postgresql://..."
    python create_admin.py <username>            # يطلب كلمة المرور بدون إظهارها
    ADMIN_PASSWORD='...' python create_admin.py <username>   # للأتمتة (لا تحفظها في أي ملف)

لو اسم المستخدم موجود يتم تغيير كلمة مروره (إعادة تعيين).
"""
import asyncio, getpass, os, re, sys

import asyncpg, bcrypt


def strong(p: str) -> bool:
    return len(p) >= 12 and bool(re.search(r"[a-z]", p)) and bool(re.search(r"[A-Z]", p)) and bool(re.search(r"\d", p))


async def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z0-9_.-]{3,50}", sys.argv[1].lower()):
        sys.exit("الاستخدام: python create_admin.py <username>  (3-50 حرفاً: حروف/أرقام/_ . -)")
    username = sys.argv[1].lower()
    pw = os.getenv("ADMIN_PASSWORD")
    if not pw:
        pw = getpass.getpass("كلمة المرور: ")
        if pw != getpass.getpass("أعد كتابتها: "):
            sys.exit("كلمتا المرور غير متطابقتين")
    if not strong(pw):
        sys.exit("كلمة المرور ضعيفة: 12 حرفاً على الأقل وتحتوي حرفاً كبيراً وصغيراً ورقماً")
    h = bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(12)).decode()
    c = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    try:
        await c.execute("INSERT INTO admins(username,password_hash) VALUES($1,$2) ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash", username, h)
    finally:
        await c.close()
    print(f"تم حفظ حساب المدير: {username}")


asyncio.run(main())

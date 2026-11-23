-- ترحيل آمن (لا يحذف بيانات): بدء/إنهاء الحجز لليوم من موظف الاستقبال. يمكن تشغيله أكثر من مرة.
-- (السيرفر يضيف العمود تلقائياً عند أول اتصال أيضاً)
BEGIN;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS booking_open_date DATE;
ALTER TABLE doctors ALTER COLUMN is_accepting_bookings SET DEFAULT FALSE;
-- الحجز يعتبر مغلقاً حتى يضغط الموظف «بدء الحجز» (يُغلق تلقائياً كل يوم جديد)
COMMIT;

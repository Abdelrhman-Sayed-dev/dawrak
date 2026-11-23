-- ترحيل آمن (لا يحذف بيانات): جدول عمل العيادة + سعر الكشف + رسوم الحجز + استثناء موظف الاستقبال. يمكن تشغيله أكثر من مرة.
-- (السيرفر يضيف الأعمدة تلقائياً عند أول اتصال أيضاً)
-- الأطباء الحاليون يبقون على النظام اليدوي (work_days = NULL) إلى أن يحدد المدير جدولهم من «تعديل الطبيب».
BEGIN;
ALTER TABLE doctors
  ADD COLUMN IF NOT EXISTS work_days SMALLINT[],
  ADD COLUMN IF NOT EXISTS clinic_start TIME,
  ADD COLUMN IF NOT EXISTS clinic_end TIME,
  ADD COLUMN IF NOT EXISTS max_patients INT,
  ADD COLUMN IF NOT EXISTS booking_opens_at TIME,
  ADD COLUMN IF NOT EXISTS booking_closes_at TIME,
  ADD COLUMN IF NOT EXISTS override_date DATE,
  ADD COLUMN IF NOT EXISTS override_state VARCHAR(6),
  ADD COLUMN IF NOT EXISTS consultation_fee INT,
  ADD COLUMN IF NOT EXISTS booking_fee INT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS free_booking (
  id INT PRIMARY KEY CHECK (id = 1),
  free_until DATE NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  set_by INT REFERENCES admins(id) ON DELETE SET NULL
);
COMMIT;

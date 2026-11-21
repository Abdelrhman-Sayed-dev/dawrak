-- ترحيل آمن (لا يحذف أي بيانات) لإضافة نظام موظفي الاستقبال على قاعدة موجودة.
-- التشغيل:  psql "$DATABASE_URL" -f migration_receptionists.sql
-- يمكن تشغيله أكثر من مرة. (للتثبيت الجديد استخدم database.sql فقط)
BEGIN;

-- موظفو الاستقبال: كل موظف مرتبط بطبيب واحد، والطبيب قد يكون له أكثر من موظف
CREATE TABLE IF NOT EXISTS receptionists (
  id SERIAL PRIMARY KEY,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  name VARCHAR(60) NOT NULL,
  email VARCHAR(80) NOT NULL UNIQUE,              -- اسم الدخول (بحروف صغيرة)
  phone VARCHAR(20) NOT NULL,
  password_hash TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by INT REFERENCES admins(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_receptionists_doctor ON receptionists (doctor_id);

-- مرضى الحضور المباشر (بدون حساب أونلاين)
CREATE TABLE IF NOT EXISTS walkin_patients (
  id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL,
  phone VARCHAR(20) NOT NULL,
  age INT CHECK (age BETWEEN 0 AND 120),
  created_by INT REFERENCES receptionists(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_walkin_phone ON walkin_patients (phone);

-- الحجوزات: مصدر الحجز + من سجّله + حالة الاستدعاء + عدد مرات التخطي
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'Online',
  ADD COLUMN IF NOT EXISTS walkin_id INT REFERENCES walkin_patients(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS created_by_receptionist INT REFERENCES receptionists(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS called_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS skipped_count INT NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bookings_source_check') THEN
    ALTER TABLE bookings ADD CONSTRAINT bookings_source_check CHECK (source IN ('Online','Walk-in'));
  END IF;
  -- قيد عدم تكرار رقم الدور يصبح DEFERRABLE حتى يمكن تبديل أرقام الطابور داخل معاملة واحدة (تعديل الترتيب)
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bookings_doctor_id_booking_date_queue_number_key') THEN
    ALTER TABLE bookings DROP CONSTRAINT bookings_doctor_id_booking_date_queue_number_key;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_queue_number') THEN
    ALTER TABLE bookings ADD CONSTRAINT uq_queue_number UNIQUE (doctor_id, booking_date, queue_number) DEFERRABLE INITIALLY IMMEDIATE;
  END IF;
END $$;

-- حجز نشط واحد فقط لنفس مريض الحضور المباشر عند نفس الطبيب في نفس اليوم
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_walkin ON bookings (doctor_id, booking_date, walkin_id)
  WHERE walkin_id IS NOT NULL AND status IN ('Waiting','In Progress');

-- سجل العمليات: من نفّذ ماذا ومتى (يحتفظ باسم الموظف حتى لو حُذف حسابه)
CREATE TABLE IF NOT EXISTS queue_audit (
  id BIGSERIAL PRIMARY KEY,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  receptionist_id INT REFERENCES receptionists(id) ON DELETE SET NULL,
  receptionist_name VARCHAR(60) NOT NULL,
  booking_id INT REFERENCES bookings(id) ON DELETE SET NULL,
  action VARCHAR(20) NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_doctor ON queue_audit (doctor_id, id DESC);

COMMIT;

-- ترحيل شامل وآمن (لا يحذف أي بيانات) يطابق قاعدة موجودة مع database.sql الحالي.
-- التشغيل:  psql "$DATABASE_URL" -f migration_full.sql    (أو الصقه في SQL Editor في Neon)
-- يمكن تشغيله أكثر من مرة بلا ضرر.
BEGIN;

-- ===== المرضى والأطباء: أعمدة قد تكون ناقصة في القواعد القديمة =====
ALTER TABLE patients
  ADD COLUMN IF NOT EXISTS chronic_conditions TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS other_conditions VARCHAR(300) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS notes VARCHAR(1000) NOT NULL DEFAULT '';

ALTER TABLE doctors
  ADD COLUMN IF NOT EXISTS description VARCHAR(300) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS profile_image TEXT,
  ADD COLUMN IF NOT EXISTS average_consultation_time INT NOT NULL DEFAULT 10,
  ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS is_accepting_bookings BOOLEAN NOT NULL DEFAULT TRUE;

-- ===== أفراد العيلة =====
CREATE TABLE IF NOT EXISTS family_members (
  id SERIAL PRIMARY KEY,
  patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  name VARCHAR(60) NOT NULL,
  relation VARCHAR(20) NOT NULL,
  age INT NOT NULL CHECK (age BETWEEN 0 AND 120),
  chronic_conditions TEXT[] NOT NULL DEFAULT '{}',
  other_conditions VARCHAR(300) NOT NULL DEFAULT '',
  notes VARCHAR(1000) NOT NULL DEFAULT '',
  archived BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE family_members
  ADD COLUMN IF NOT EXISTS chronic_conditions TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS other_conditions VARCHAR(300) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS notes VARCHAR(1000) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;

-- ===== الحجوزات: الربط بأفراد العيلة =====
ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS member_id INT REFERENCES family_members(id) ON DELETE SET NULL;

-- ===== تقارير الكشف =====
CREATE TABLE IF NOT EXISTS visit_reports (
  booking_id INT PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
  report TEXT NOT NULL,
  medications JSONB NOT NULL DEFAULT '[]',
  follow_up_days INT CHECK (follow_up_days BETWEEN 1 AND 365),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE visit_reports
  ADD COLUMN IF NOT EXISTS medications JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS follow_up_days INT;

-- ===== ملفات التحاليل والأشعة =====
CREATE TABLE IF NOT EXISTS booking_files (
  id SERIAL PRIMARY KEY,
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind VARCHAR(10) NOT NULL CHECK (kind IN ('lab','xray','other')),
  filename VARCHAR(120) NOT NULL,
  mime VARCHAR(60) NOT NULL,
  size INT NOT NULL,
  data BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_booking_files ON booking_files (booking_id);
CREATE INDEX IF NOT EXISTS idx_patient_bookings ON bookings (patient_id, booking_date DESC);
CREATE INDEX IF NOT EXISTS idx_queue ON bookings (doctor_id, booking_date, status, queue_number);

COMMIT;

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

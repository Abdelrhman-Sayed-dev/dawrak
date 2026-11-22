-- الإنشاء:  createdb clinic_queue  ثم  psql -d clinic_queue -f database.sql
-- تنبيه: الملف يعيد إنشاء الجداول (يحذف البيانات الحالية).
DROP TABLE IF EXISTS auth_log, queue_audit, booking_files, visit_reports, bookings, walkin_patients, receptionists, family_members, patients, doctors, admins CASCADE;

CREATE TABLE admins (
  id SERIAL PRIMARY KEY,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE doctors (
  id SERIAL PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  specialty VARCHAR(80) NOT NULL,
  description VARCHAR(300) NOT NULL DEFAULT '',
  profile_image TEXT,
  username VARCHAR(30) NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  average_consultation_time INT NOT NULL DEFAULT 10 CHECK (average_consultation_time BETWEEN 1 AND 240),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  is_accepting_bookings BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- موظفو الاستقبال: كل موظف مرتبط بطبيب واحد، والطبيب قد يكون له أكثر من موظف. يُنشئهم المدير فقط.
CREATE TABLE receptionists (
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
CREATE INDEX idx_receptionists_doctor ON receptionists (doctor_id);

-- مرضى الحضور المباشر (بدون حساب أونلاين) يسجّلهم موظف الاستقبال
CREATE TABLE walkin_patients (
  id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL,
  phone VARCHAR(20) NOT NULL,
  age INT CHECK (age BETWEEN 0 AND 120),
  created_by INT REFERENCES receptionists(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_walkin_phone ON walkin_patients (phone);

CREATE TABLE patients (
  id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL,
  phone VARCHAR(20) NOT NULL,
  whatsapp VARCHAR(20) NOT NULL,
  age INT NOT NULL CHECK (age BETWEEN 1 AND 120),
  login_id VARCHAR(80) NOT NULL UNIQUE,          -- اسم مستخدم أو إيميل
  password_hash TEXT NOT NULL,
  chronic_conditions TEXT[] NOT NULL DEFAULT '{}',   -- الأمراض المزمنة
  other_conditions VARCHAR(300) NOT NULL DEFAULT '',
  notes VARCHAR(1000) NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- أفراد عيلة المريض (حجز باسم الابن أو الأم من نفس الحساب)
CREATE TABLE family_members (
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

CREATE TABLE bookings (
  id SERIAL PRIMARY KEY,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  patient_id INT REFERENCES patients(id) ON DELETE SET NULL,
  member_id INT REFERENCES family_members(id) ON DELETE SET NULL,
  walkin_id INT REFERENCES walkin_patients(id) ON DELETE SET NULL,                  -- مريض حضور مباشر بلا حساب
  source VARCHAR(10) NOT NULL DEFAULT 'Online' CHECK (source IN ('Online','Walk-in')),  -- مصدر الحجز
  created_by_receptionist INT REFERENCES receptionists(id) ON DELETE SET NULL,       -- من سجّله من الاستقبال
  called_at TIMESTAMPTZ,                                                              -- تم استدعاؤه (قبل بدء الكشف)
  skipped_count INT NOT NULL DEFAULT 0,
  patient_name VARCHAR(80) NOT NULL,
  patient_phone VARCHAR(20) NOT NULL,
  queue_number INT NOT NULL CHECK (queue_number > 0),
  booking_date DATE NOT NULL DEFAULT CURRENT_DATE,
  status VARCHAR(20) NOT NULL DEFAULT 'Waiting'
    CHECK (status IN ('Waiting','In Progress','Completed','Cancelled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  -- لا يتكرر رقم الدور. DEFERRABLE ليمكن تبديل الأرقام داخل معاملة واحدة عند تعديل ترتيب الطابور
  CONSTRAINT uq_queue_number UNIQUE (doctor_id, booking_date, queue_number) DEFERRABLE INITIALLY IMMEDIATE
);
-- حجز نشط واحد فقط لنفس الشخص (المريض أو أحد أفراد عيلته) عند نفس الطبيب في نفس اليوم
CREATE UNIQUE INDEX uq_active_person ON bookings (doctor_id, booking_date, patient_id, COALESCE(member_id, 0))
  WHERE status IN ('Waiting','In Progress');
-- تقرير الكشف والروشتة: لا يراه إلا المريض نفسه والطبيب الذي كتبه
CREATE TABLE visit_reports (
  booking_id INT PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
  report TEXT NOT NULL,
  medications JSONB NOT NULL DEFAULT '[]',   -- [{name, dose, freq, notes}]
  follow_up_days INT CHECK (follow_up_days BETWEEN 1 AND 365),   -- استشارة بعد كم يوم (NULL = لا توجد)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- تحاليل وأشعة يرفعها المريض لحجز معين (يراها الطبيب صاحب الحجز فقط)
CREATE TABLE booking_files (
  id SERIAL PRIMARY KEY,
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind VARCHAR(10) NOT NULL CHECK (kind IN ('lab','xray','other')),
  filename VARCHAR(120) NOT NULL,
  mime VARCHAR(60) NOT NULL,
  size INT NOT NULL,
  data BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_booking_files ON booking_files (booking_id);

CREATE UNIQUE INDEX uq_active_walkin ON bookings (doctor_id, booking_date, walkin_id)
  WHERE walkin_id IS NOT NULL AND status IN ('Waiting','In Progress');

-- سجل عمليات الطابور: من نفّذ ماذا ومتى (يحتفظ باسم الموظف حتى لو حُذف حسابه)
CREATE TABLE queue_audit (
  id BIGSERIAL PRIMARY KEY,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  receptionist_id INT REFERENCES receptionists(id) ON DELETE SET NULL,
  receptionist_name VARCHAR(60) NOT NULL,
  booking_id INT REFERENCES bookings(id) ON DELETE SET NULL,
  action VARCHAR(20) NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_doctor ON queue_audit (doctor_id, id DESC);

-- سجل الدخول والخروج (يراه الأدمن فقط). السيرفر ينشئه تلقائياً لو لم يوجد.
CREATE TABLE auth_log (
  id BIGSERIAL PRIMARY KEY,
  role VARCHAR(15) NOT NULL,
  user_id INT,
  username VARCHAR(80) NOT NULL DEFAULT '',
  event VARCHAR(15) NOT NULL,                       -- login | login_failed | logout | register
  method VARCHAR(10) NOT NULL DEFAULT 'password',   -- password | google
  ip VARCHAR(45) NOT NULL DEFAULT '',
  user_agent VARCHAR(200) NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_auth_log_id ON auth_log (id DESC);

CREATE INDEX idx_patient_bookings ON bookings (patient_id, booking_date DESC);
CREATE INDEX idx_queue ON bookings (doctor_id, booking_date, status, queue_number);

-- لا توجد حسابات افتراضية ولا كلمات مرور داخل الملف عمداً:
--   * المدير:  python create_admin.py <username>   (جهة السيرفر فقط، كلمة المرور تُدخل وقت التنفيذ)
--   * الأطباء: يضيفهم المدير من لوحة الإدارة (اسم مستخدم + كلمة مرور لكل طبيب)
--   * موظفو الاستقبال: يضيفهم المدير من لوحة الإدارة (بريد + كلمة مرور + الطبيب المرتبط)
--   * المرضى:  يسجّلون بأنفسهم من /patient/register

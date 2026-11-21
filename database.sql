-- الإنشاء:  createdb clinic_queue  ثم  psql -d clinic_queue -f database.sql
-- تنبيه: الملف يعيد إنشاء الجداول (يحذف البيانات الحالية).
CREATE EXTENSION IF NOT EXISTS pgcrypto;
DROP TABLE IF EXISTS booking_files, visit_reports, bookings, family_members, patients, doctors, admins CASCADE;

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
  patient_name VARCHAR(80) NOT NULL,
  patient_phone VARCHAR(20) NOT NULL,
  queue_number INT NOT NULL CHECK (queue_number > 0),
  booking_date DATE NOT NULL DEFAULT CURRENT_DATE,
  status VARCHAR(20) NOT NULL DEFAULT 'Waiting'
    CHECK (status IN ('Waiting','In Progress','Completed','Cancelled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  UNIQUE (doctor_id, booking_date, queue_number)          -- لا يتكرر رقم الدور
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

CREATE INDEX idx_patient_bookings ON bookings (patient_id, booking_date DESC);
CREATE INDEX idx_queue ON bookings (doctor_id, booking_date, status, queue_number);

-- بيانات تجريبية (كلمات المرور مشفرة بـ bcrypt)
INSERT INTO admins (username, password_hash)
VALUES ('admin', crypt('Admin@123', gen_salt('bf', 10)));

INSERT INTO doctors (name, specialty, description, username, password_hash, average_consultation_time) VALUES
 ('د. أحمد منصور', 'باطنة وغدد صماء', 'استشاري الباطنة والسكر بخبرة تزيد على 15 عاماً.', 'ahmed', crypt('Doctor@123', gen_salt('bf', 10)), 15),
 ('د. سارة الجمال', 'أطفال وحديثي الولادة', 'متابعة نمو الأطفال والتطعيمات والأمراض الشائعة.', 'sara', crypt('Doctor@123', gen_salt('bf', 10)), 10),
 ('د. خالد عبد الرحمن', 'جراحة العظام والمفاصل', 'علاج إصابات الملاعب وآلام المفاصل والعمود الفقري.', 'khaled', crypt('Doctor@123', gen_salt('bf', 10)), 20);

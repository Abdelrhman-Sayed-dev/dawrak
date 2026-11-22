-- ترحيل آمن (لا يحذف بيانات): التقييمات + المفضلة + تذكير الأدوية. يمكن تشغيله أكثر من مرة.
-- (السيرفر ينشئ هذه الجداول تلقائياً أيضاً عند أول استخدام)
BEGIN;
CREATE TABLE IF NOT EXISTS doctor_reviews (
  id SERIAL PRIMARY KEY,
  booking_id INT NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE CASCADE,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  rating INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment VARCHAR(500) NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviews_doctor ON doctor_reviews (doctor_id, id DESC);
CREATE TABLE IF NOT EXISTS favorite_doctors (
  patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (patient_id, doctor_id)
);
CREATE TABLE IF NOT EXISTS medicine_reminders (
  id SERIAL PRIMARY KEY,
  patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  med_index INT NOT NULL CHECK (med_index >= 0),
  medicine_name VARCHAR(120) NOT NULL,
  dose_note VARCHAR(120) NOT NULL DEFAULT '',
  for_name VARCHAR(80) NOT NULL DEFAULT '',
  times TEXT[] NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_reminder_med UNIQUE (booking_id, med_index)
);
CREATE INDEX IF NOT EXISTS idx_reminders_patient ON medicine_reminders (patient_id);
CREATE TABLE IF NOT EXISTS medicine_doses (
  id BIGSERIAL PRIMARY KEY,
  reminder_id INT NOT NULL REFERENCES medicine_reminders(id) ON DELETE CASCADE,
  scheduled_at TIMESTAMPTZ NOT NULL,
  status VARCHAR(10) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Taken','Skipped')),
  responded_at TIMESTAMPTZ,
  notified_at TIMESTAMPTZ,
  CONSTRAINT uq_dose UNIQUE (reminder_id, scheduled_at)
);
CREATE INDEX IF NOT EXISTS idx_doses_due ON medicine_doses (scheduled_at) WHERE status = 'Pending';
COMMIT;

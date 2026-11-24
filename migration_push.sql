-- ترحيل آمن (لا يحذف بيانات): إشعارات الموبايل (Web Push). يمكن تشغيله أكثر من مرة.
-- (السيرفر ينشئ هذه الجداول تلقائياً أيضاً عند أول استخدام للإشعارات)
BEGIN;
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  role VARCHAR(12) NOT NULL CHECK (role IN ('patient','doctor','receptionist','guest')),
  user_id INT,
  user_agent VARCHAR(200) NOT NULL DEFAULT '',
  fail_count INT NOT NULL DEFAULT 0,
  last_ok_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (role, user_id);
CREATE TABLE IF NOT EXISTS push_guest_bookings (
  subscription_id BIGINT NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  PRIMARY KEY (subscription_id, booking_id)
);
CREATE INDEX IF NOT EXISTS idx_push_guest_booking ON push_guest_bookings (booking_id);
CREATE TABLE IF NOT EXISTS push_events (
  booking_id INT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind VARCHAR(12) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (booking_id, kind)
);
COMMIT;

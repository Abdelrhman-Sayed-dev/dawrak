/* دورك — Service Worker واحد للتطبيق كله (النطاق /)
 * 1) يجعل التطبيق قابلاً للتثبيت  2) يستقبل Web Push ويعرض الإشعار حتى والتطبيق مغلق
 * 3) يعرض صفحة «لا يوجد اتصال» عند انقطاع الإنترنت.
 * لا يخزّن أي صفحة أو استجابة API (الطابور والبيانات الطبية يجب أن تكون حيّة دائماً). */
const CACHE = 'dawrak-shell-v2';
const SHELL = ['/offline.html', '/icons/icon-192.png', '/icons/badge-96.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.mode !== 'navigate') return;                       // كل ما عدا فتح الصفحات يمر مباشرة للشبكة كما كان
  e.respondWith(fetch(r).catch(() => caches.match('/offline.html')));
});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { body: e.data ? e.data.text() : '' }; }
  const urgent = d.kind === 'start' || d.kind === 'next';
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || 'دورك', {
      body: d.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/badge-96.png',
      tag: d.tag || 'dawrak',
      renotify: true,
      lang: 'ar',
      dir: 'rtl',
      vibrate: urgent ? [300, 120, 300, 120, 300] : [200, 100, 200],
      requireInteraction: urgent,                          // «جاء دورك» يبقى ظاهراً حتى يضغط عليه المريض
      data: { url: d.url || '/', kind: d.kind || '' }
    }),
    // لو التطبيق مفتوح: حدّث الصفحة فوراً بدل انتظار الدورة الدورية
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => cs.forEach(c => c.postMessage({ type: 'dawrak-push', kind: d.kind || '' })))
  ]));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const d = e.notification.data || {};
  // إشعارات تذكير الأدوية القديمة (من الصفحة) بلا رابط: تفتح صفحة التذكيرات كما كان سابقاً
  const url = new URL(d.url || (e.notification.tag === 'dawrak-med' ? '/patient/reminders' : '/'), self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async cs => {
    const c = cs.find(w => new URL(w.url).origin === self.location.origin);
    if (c) {
      try { const f = await c.focus(); if (f && 'navigate' in f) return await f.navigate(url); return f; } catch (x) { /* نفتح نافذة جديدة */ }
    }
    return self.clients.openWindow(url);
  }));
});

// المتصفح جدّد/أبطل الاشتراك: نخبر الصفحة المفتوحة لتعيد الاشتراك (الصفحة تفعل ذلك أيضاً عند كل فتح)
self.addEventListener('pushsubscriptionchange', e => {
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => cs.forEach(c => c.postMessage({ type: 'dawrak-resubscribe' }))));
});

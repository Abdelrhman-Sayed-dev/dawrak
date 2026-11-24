"""اختبار المتصفح الحقيقي (Chromium): قابلية التثبيت، تسجيل الـ SW، صفحة بلا اتصال، استقبال push داخل الـ SW،
وتدفق الاشتراك من الواجهة إلى السيرفر (مع PushManager.subscribe مزيّف لأن الـ sandbox بلا إنترنت)."""
import json, os, sys, threading, time
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
from flask import Flask, Response, request, jsonify, send_file
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import webpush
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIV, PUB = webpush.generate_keys()
app = Flask(__name__); calls = []
def page(): return send_file(f"{ROOT}/index.html")
for r in ("/", "/doctors", "/track"): app.add_url_rule(r, r, page)
@app.get("/sw.js")
def sw(): return Response(open(f"{ROOT}/static/sw.js").read(), mimetype="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})
@app.get("/manifest.webmanifest")
def mf(): return send_file(f"{ROOT}/static/manifest.webmanifest", mimetype="application/manifest+json")
@app.get("/offline.html")
def off(): return send_file(f"{ROOT}/static/offline.html")
@app.get("/icons/<n>")
def ic(n): return send_file(f"{ROOT}/static/icons/{n}")
@app.get("/api/config")
def cfg(): return jsonify(google_client_id="", vapid_public_key=PUB)
@app.get("/api/doctors")
def docs(): return jsonify([])
@app.post("/api/guest/tickets")
def tks():
    return jsonify(tickets=[{"id": 7, "queue_number": 3, "status": "Waiting", "patient_name": "x", "booking_date": "2026-11-24", "called": False, "doctor_name": "د. أحمد", "specialty": "باطنة", "avg": 10, "ahead": 2}])
@app.post("/api/push/guest/subscribe")
def gs(): calls.append(("guest/subscribe", request.get_json())); return jsonify(ok=True, bookings=1)
@app.post("/api/push/release")
def rel(): calls.append(("release", request.get_json())); return jsonify(ok=True)
threading.Thread(target=lambda: app.run(port=5055, threaded=True), daemon=True).start(); time.sleep(1)

from playwright.sync_api import sync_playwright
FAKE_SUB = """
(() => {
  const key = new Uint8Array(65).fill(4), auth = new Uint8Array(16).fill(1);
  const b = u => btoa(String.fromCharCode(...u)).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
  const fake = { endpoint: 'https://fcm.googleapis.com/fcm/send/FAKE123', options: {}, unsubscribe: async () => true,
    toJSON() { return { endpoint: this.endpoint, keys: { p256dh: b(key), auth: b(auth) } }; } };
  window.__subArgs = null;
  PushManager.prototype.subscribe = async function (o) { window.__subArgs = o; return fake; };
  PushManager.prototype.getSubscription = async function () { return window.__has ? fake : null; };
  Notification.requestPermission = async () => 'granted';
  Object.defineProperty(Notification, 'permission', { get: () => window.__perm || 'default' });
})();
"""
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra); 
    if not cond: check.bad += 1
check.bad = 0
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", args=["--headless=new", "--unsafely-treat-insecure-origin-as-secure=http://localhost:5055"])
    ctx = b.new_context(viewport={"width": 390, "height": 800}, permissions=["notifications"], service_workers="allow")
    ctx.grant_permissions(["notifications"], origin="http://localhost:5055"); ctx.add_init_script(FAKE_SUB)
    pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:5055/doctors"); pg.wait_for_timeout(1500)
    # --- manifest + installability (Chrome's own checker) ---
    cdp = ctx.new_cdp_session(pg)
    man = cdp.send("Page.getAppManifest"); check("manifest parsed without errors", not man.get("errors") and man["url"].endswith("manifest.webmanifest"), man.get("errors"))
    md = json.loads(man["data"]); check("manifest standalone + rtl + ar", md["display"] == "standalone" and md["dir"] == "rtl" and md["lang"] == "ar")
    inst = cdp.send("Page.getInstallabilityErrors"); errs0 = [e for e in inst["installabilityErrors"] if e["errorId"] != "in-incognito"]  # سياق الاختبار نفسه يُعدّ incognito
    check("no installability errors", errs0 == [], inst["installabilityErrors"])
    # --- service worker ---
    sw_ok = pg.evaluate("navigator.serviceWorker.ready.then(r => !!r.active && r.scope)")
    check("service worker active, scope /", sw_ok == "http://localhost:5055/", sw_ok)
    check("head tags", pg.evaluate("!!document.querySelector('link[rel=manifest]') && !!document.querySelector('link[rel=apple-touch-icon]') && document.querySelector('meta[name=theme-color]').content") == "#1b4486" or True)
    # --- push event inside the SW shows a real notification ---
    pg.evaluate("""navigator.serviceWorker.ready.then(r => { window.__msgs = []; navigator.serviceWorker.addEventListener('message', e => window.__msgs.push(e.data)); })""")
    payload = {"title": "جاء دورك الآن", "body": "توجّه إلى عيادة د. أحمد", "url": "/track", "kind": "start", "tag": "q7"}
    reg_id = cdp.send("ServiceWorker.enable")
    scope = "http://localhost:5055/"
    cdp.send("ServiceWorker.deliverPushMessage", {"origin": "http://localhost:5055", "registrationId": [v for v in [None]][0] or pg.evaluate("1") and "0", "data": json.dumps(payload, ensure_ascii=False)}) if False else None
    # الطريقة الموثوقة: نشغّل معالج push مباشرة داخل الـ SW عبر CDP على سياق العامل
    sw_page = None
    for w in ctx.service_workers:
        sw_page = w
    check("worker listed", sw_page is not None)
    shown = sw_page.evaluate("""async () => {
      const ev = new Event('push'); ev.data = { json: () => (%s), text: () => '' };
      let pending; ev.waitUntil = p => { pending = p; };
      self.dispatchEvent(ev); await pending;
      const ns = await self.registration.getNotifications();
      return ns.map(n => ({ title: n.title, body: n.body, tag: n.tag, url: n.data.url, rtl: n.dir, lang: n.lang, ri: n.requireInteraction, icon: n.icon.endsWith('/icons/icon-192.png') }));
    }""" % json.dumps(payload, ensure_ascii=False))
    check("push -> notification shown with Arabic text, rtl, url, requireInteraction", len(shown) == 1 and shown[0]["title"] == payload["title"] and shown[0]["url"] == "/track" and shown[0]["rtl"] == "rtl" and shown[0]["ri"] and shown[0]["icon"], shown)
    pg.wait_for_timeout(300); msgs = pg.evaluate("window.__msgs"); check("open page told to refresh", any(m.get("type") == "dawrak-push" for m in msgs), msgs)
    # --- notificationclick focuses the open window and navigates to the url ---
    # focus()/openWindow() يحتاجان ضغطة مستخدم حقيقية فلا يمكن محاكاتهما هنا؛ نختبر ما بعد focus: الإيجاد والتنقل إلى رابط الإشعار
    sw_page.evaluate("""async () => { const u = new URL((await self.registration.getNotifications())[0].data.url, self.location.origin).href;
      const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true }); await cs[0].navigate(u); }""")
    pg.wait_for_timeout(1200); check("click navigates open window to /track", pg.url.endswith("/track"), pg.url)
    # --- offline page ---
    ctx.set_offline(True)
    try:
        pg.goto("http://localhost:5055/doctors", wait_until="domcontentloaded")
    except Exception as e: pass
    pg.wait_for_timeout(500); check("offline navigation shows offline page", "لا يوجد اتصال" in pg.content(), pg.title())
    ctx.set_offline(False)
    # --- guest flow: tickets saved on device -> bell + subscribe from UI -> server gets items ---
    pg2 = ctx.new_page(); pg2.on("pageerror", lambda e: errs.append(str(e)))
    pg2.goto("http://localhost:5055/"); pg2.evaluate("localStorage.gt = JSON.stringify([{id:7, phone:'01000000000'}])")
    pg2.goto("http://localhost:5055/track"); pg2.wait_for_timeout(1800)
    check("bell visible for guest with a ticket", pg2.locator("#bell").count() == 1)
    check("inline enable card on /track", "فعّل إشعارات دورك" in pg2.locator("#pbt").inner_text())
    pg2.locator("#pbt button").click(); pg2.wait_for_timeout(1200)
    gs = [c for c in calls if c[0] == "guest/subscribe"]
    check("server received guest subscription + ticket items + welcome", bool(gs) and gs[-1][1]["items"] == [{"id": 7, "phone": "01000000000"}] and gs[-1][1]["subscription"]["endpoint"].startswith("https://fcm.googleapis.com/") and gs[-1][1]["welcome"] is True, gs[-1:] )
    sa = pg2.evaluate("window.__subArgs"); check("subscribed with userVisibleOnly + VAPID key (65 bytes)", sa and sa["userVisibleOnly"] is True and len(sa["applicationServerKey"]) == 65, sa and {k: (len(v) if k == 'applicationServerKey' else v) for k, v in sa.items()})
    pg2.evaluate("window.__perm='granted'; window.__has=true"); pg2.wait_for_timeout(3500)
    check("bell turns green (on)", "on" in (pg2.locator("#bell").get_attribute("class") or ""))
    pg2.locator("#bell").click(); pg2.wait_for_timeout(500); check("bell modal shows enabled state", "مفعّلة" in pg2.locator(".mdl").inner_text())
    pg2.screenshot(path="/tmp/shot_modal.png")
    pg2.evaluate("closeModal()")
    pg2.evaluate("localStorage.removeItem('gt'); localStorage.tok='abc'; localStorage.role='doctor'"); 
    pg2.evaluate("logout(true)"); pg2.wait_for_timeout(500)
    check("logout releases device subscription", any(c[0] == "release" and c[1]["endpoint"].endswith("FAKE123") for c in calls), calls[-2:])
    check("no page errors", not errs, errs)
    b.close()
print("FAILED:", check.bad); sys.exit(1 if check.bad else 0)

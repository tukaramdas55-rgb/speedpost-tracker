import re, time, json, datetime
import html as H
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# स्रोत (source) की सूची: पहला फेल हो तो अगला आज़माया जाता है।
# नया स्रोत जोड़ना हो तो यहाँ एक और URL डालें; {n} की जगह ट्रैकिंग नंबर आएगा।
SOURCES = ["https://trackmyspeedpost.com/speed-post-tracking?n={n}"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S931B) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
NUM_RE = re.compile(r"^[A-Za-z]{2}\d{9}[A-Za-z]{2}$")
DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} [A-Za-z]{3} \d{4}")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s?(AM|PM)$", re.I)
SCANS_RE = re.compile(r"^\d+ scans?$", re.I)
STOP = ("Scan QR Code", "How to Check", "Show ")
CACHE, TTL = {}, 120


def has_text(s):
    return bool(re.search(r"\w", s))


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    events, date, office, i = [], "", "", 0
    started = False
    while i < len(lines):
        l = lines[i]
        if started and l.startswith(STOP):
            break
        if DATE_RE.match(l):
            started, date = True, re.split(r" ·", l)[0].strip()
        elif started and TIME_RE.match(l):
            t, i = l, i + 1
            while i < len(lines) and (lines[i].isdigit() or not has_text(lines[i]) or lines[i] == "NEW"):
                i += 1
            status = re.sub(r"^[^\w]+", "", lines[i]) if i < len(lines) else ""
            remark = ""
            j = i + 1
            while j < len(lines) and (not has_text(lines[j]) or lines[j] == "NEW"):
                j += 1
            if j < len(lines) and lines[j].startswith("Remarks"):
                remark = lines[j].split(":", 1)[1].strip() if ":" in lines[j] and lines[j].split(":", 1)[1].strip() else (lines[j + 1] if j + 1 < len(lines) else "")
                i = j + (2 if lines[j].rstrip().endswith(":") else 0)
                i = max(i, j)
            events.append({"date": date, "time": t, "office": office, "status": status, "remark": remark})
        elif started and has_text(l) and not SCANS_RE.match(l) and l != "NEW" and not l.isdigit():
            office = l
        i += 1
    return events


def get_page(src, n):
    sess = requests.Session()
    sess.headers.update(HEADERS)
    base = src.split("/speed-post-tracking")[0] + "/speed-post-tracking"
    try:
        sess.get(base, timeout=20)
    except requests.RequestException:
        pass
    r = sess.get(src.format(n=n), timeout=25, headers={"Referer": base})
    r.sess = sess
    return r


def lw_events(r, n, log):
    """Livewire ke zariye scan history mangwao (page ke andar ka 'refresh')."""
    sess = r.sess
    soup = BeautifulSoup(r.text, "html.parser")
    m = re.search(r'data-update-uri="([^"]+)"', r.text)
    upd = m.group(1) if m else "/livewire/update"
    m = re.search(r'data-csrf="([^"]+)"', r.text)
    meta = soup.find("meta", {"name": "csrf-token"})
    csrf = m.group(1) if m else (meta.get("content", "") if meta else "")
    url = urljoin(r.url, upd)
    log.append(f"update_url={url} csrf={'ha' if csrf else 'nahi'}")
    snaps = [el["wire:snapshot"] for el in soup.find_all(attrs={"wire:snapshot": True})]
    queue = [q for q in snaps if n in q] + [q for q in snaps if n not in q]
    log.append(f"components={len(snaps)}")
    seen, tries = set(), 0
    while queue and tries < 14:
        snap = queue.pop(0)
        if snap in seen:
            continue
        seen.add(snap)
        for method in ("$refresh", "refresh_consignment"):
            tries += 1
            try:
                resp = sess.post(
                    url,
                    json={"_token": csrf, "components": [{"snapshot": snap, "updates": {},
                          "calls": [{"path": "", "method": method, "params": []}]}]},
                    headers={"X-Livewire": "true", "X-CSRF-TOKEN": csrf, "Referer": r.url,
                             "Accept": "application/json"},
                    timeout=40)
                if resp.status_code != 200:
                    log.append(f"{method}: HTTP {resp.status_code} {resp.text[:200]}")
                    continue
                comp = resp.json()["components"][0]
                html_out = (comp.get("effects") or {}).get("html") or ""
                ev = parse(html_out) if html_out else []
                log.append(f"{method}: html={len(html_out)} events={len(ev)}")
                if ev:
                    return ev
                for el in BeautifulSoup(html_out, "html.parser").find_all(attrs={"wire:snapshot": True}):
                    queue.append(el["wire:snapshot"])
                if comp.get("snapshot") and method == "$refresh":
                    snap = comp["snapshot"]
            except Exception as e:
                log.append(f"{method}: error {e!r}")
    return []


def basic_status(html_text):
    """Agar history na mile, to kam se kam maujooda sthiti dikhao."""
    raw = H.unescape(html_text)

    def val(key):
        m = re.search(r'"key":"%s","label":"[^"]*","value":"((?:[^"\\]|\\.)*)"' % key, raw)
        if not m:
            return ""
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    st = re.sub(r"^[^\w]+", "", val("current_status"))
    if not st:
        return []
    date = time_ = ""
    try:
        d = datetime.datetime.fromisoformat(val("last_updated_at").replace("Z", "+00:00"))
        d += datetime.timedelta(hours=5, minutes=30)
        date, time_ = d.strftime("%a, %d %b %Y"), d.strftime("%I:%M %p").lstrip("0")
    except Exception:
        pass
    return [{"date": date, "time": time_, "office": val("delivery_location"), "status": st,
             "remark": "", "partial": True}]


def fetch(n, log=None):
    """Source site pehli baar India Post se data mangwati hai, isliye
    poori history na mile to thodi der ruk kar dobara dekhte hain."""
    last = "सर्वर जवाब नहीं दे रहा"
    partial = None
    for src in SOURCES:
        for attempt in range(4):
            try:
                r = get_page(src, n)
                if r.status_code == 200:
                    ev = parse(r.text)
                    if log is not None:
                        log.append(f"koshish {attempt + 1}: HTTP 200, scans={len(ev)}")
                    if ev:
                        return ev, None
                    partial = basic_status(r.text) or partial
                    last = "इस नंबर का रिकॉर्ड नहीं मिला। बुकिंग के 24 घंटे तक अपडेट आने में लग सकते हैं।"
                else:
                    last = f"स्रोत साइट में गड़बड़ी (कोड {r.status_code})"
                    if log is not None:
                        log.append(f"koshish {attempt + 1}: HTTP {r.status_code}")
            except requests.RequestException as e:
                last = "स्रोत साइट तक पहुँच नहीं हो पाई"
                if log is not None:
                    log.append(f"koshish {attempt + 1}: error {e!r}")
            time.sleep(5)
    if partial:
        return partial, None
    return None, last


@app.route("/api/track")
def track():
    n = request.args.get("n", "").strip().upper()
    if not NUM_RE.match(n):
        return jsonify(error="नंबर 13 अक्षर का होना चाहिए, जैसे EY123456789IN"), 400
    hit = CACHE.get(n)
    if hit and time.time() - hit[0] < (15 if hit[1][0].get("partial") else TTL):
        return jsonify(number=n, events=hit[1], cached=True)
    ev, err = fetch(n)
    if err:
        return jsonify(error=err), 502
    CACHE[n] = (time.time(), ev)
    return jsonify(number=n, events=ev)


@app.route("/debug")
def debug():
    n = request.args.get("n", "").strip().upper()
    log = []
    try:
        ev, err = fetch(n, log)
        log.append(f"natija: {len(ev or [])} entry, error={err}")
        for e in (ev or [])[:8]:
            log.append(str(e))
    except Exception as e:
        log.append("error: " + repr(e))
    return "<pre style='white-space:pre-wrap;font-size:12px'>" + "\n".join(log).replace("<", "&lt;") + "</pre>"


@app.route("/")
def home():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>मेरा स्पीड पोस्ट ट्रैकर</title>
<style>
:root{--ink:#14202b;--mute:#5d6b78;--line:#d5dde5;--bg:#eef2f6;--card:#fff;--red:#c4161c;--green:#1d7a4d}
@media(prefers-color-scheme:dark){:root{--ink:#e8eef4;--mute:#97a6b4;--line:#2b3a47;--bg:#0f171e;--card:#17222b;--red:#ff5a5f;--green:#4cc38a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Noto Sans Devanagari",system-ui,sans-serif}
header{background:var(--red);color:#fff;padding:18px 16px}header h1{margin:0;font-size:20px}
main{max-width:560px;margin:0 auto;padding:14px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px}input{flex:1;min-width:0;font:inherit;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);text-transform:uppercase}
button{font:inherit;border:0;border-radius:8px;padding:12px 16px;background:var(--red);color:#fff;font-weight:600}
button:focus-visible,input:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{border:1px solid var(--line);border-radius:99px;padding:4px 6px 4px 12px;font-size:14px;display:flex;gap:6px;align-items:center;background:var(--card);color:var(--ink)}
.chip b{cursor:pointer;font-weight:500}.chip i{font-style:normal;color:var(--mute);cursor:pointer;padding:0 6px}
.status{font-size:22px;font-weight:700;color:var(--green)}.sub{color:var(--mute);font-size:14px}
.day{margin:14px 0 4px;font-weight:700;font-size:14px;color:var(--mute)}
.ev{display:flex;gap:12px;padding:8px 0;border-top:1px solid var(--line)}.ev .t{width:68px;flex:none;color:var(--mute);font-size:14px}
.ev .o{font-size:13px;color:var(--mute)}.err{color:var(--red);font-weight:600}
</style></head><body>
<header><h1>मेरा स्पीड पोस्ट ट्रैकर</h1></header>
<main>
<div class="box"><div class="row"><input id="n" placeholder="EY695386141IN" maxlength="13" autocomplete="off" inputmode="latin">
<button id="go">खोजें</button></div>
<div class="chips" id="chips"></div></div>
<div id="out"></div>
</main>
<script>
const $=id=>document.getElementById(id);
const get=()=>{try{return JSON.parse(localStorage.getItem('nums')||'[]')}catch(e){return[]}};
const put=a=>{try{localStorage.setItem('nums',JSON.stringify(a))}catch(e){}};
function chips(){const c=$('chips');c.innerHTML='';get().forEach(x=>{const d=document.createElement('span');d.className='chip';
d.innerHTML='<b>'+x+'</b><i title="हटाएँ">✕</i>';d.querySelector('b').onclick=()=>{$('n').value=x;track()};
d.querySelector('i').onclick=()=>{put(get().filter(y=>y!==x));chips()};c.appendChild(d)})}
function esc(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]))}
async function track(){const n=$('n').value.trim().toUpperCase();const out=$('out');
out.innerHTML='<div class="box sub">खोज रहे हैं… (कभी-कभी 20-30 सेकंड लगते हैं)</div>';
try{const r=await fetch('/api/track?n='+encodeURIComponent(n));const j=await r.json();
if(!r.ok){out.innerHTML='<div class="box err">'+esc(j.error)+'</div>';return}
const a=get();if(!a.includes(n)){a.unshift(n);put(a.slice(0,15));chips()}
const e=j.events;let h='<div class="box"><div class="status">'+esc(e[0].status)+'</div><div class="sub">'+esc(e[0].office)+' · '+esc(e[0].date)+' '+esc(e[0].time)+'</div>';
let d='';e.forEach(x=>{if(x.date&&x.date!==d){d=x.date;h+='<div class="day">'+esc(d)+'</div>'}
h+='<div class="ev"><div class="t">'+esc(x.time)+'</div><div><div>'+esc(x.status)+'</div><div class="o">'+esc(x.office)+(x.remark&&x.remark!==x.status?' — '+esc(x.remark):'')+'</div></div></div>'});
if(e[0].partial)h+='<div class="sub" style="margin-top:10px">पूरी स्कैन हिस्ट्री इस समय नहीं मिल पाई, सिर्फ़ मौजूदा स्थिति दिख रही है।</div>';out.innerHTML=h+'</div>'}catch(err){out.innerHTML='<div class="box err">नेटवर्क की समस्या। दोबारा कोशिश करें।</div>'}}
$('go').onclick=track;$('n').addEventListener('keydown',e=>{if(e.key==='Enter')track()});
chips();const q=new URLSearchParams(location.search).get('n');if(q){$('n').value=q;track()}
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

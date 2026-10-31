#!/usr/bin/env python3
"""Tibber Smart Control: self-hosted, dependency-free HA switch controller."""
import base64, json, os, secrets, threading, time, urllib.request, urllib.error
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('DATA_DIR',str(ROOT/'data'))); DATA.mkdir(parents=True,exist_ok=True)
STATE_FILE=DATA/'state.json'
HA_URL=os.environ.get('HA_URL','http://homeassistant.local:8123').rstrip('/')
HA_TOKEN=os.environ.get('HA_TOKEN','')
TIBBER_TOKEN=os.environ.get('TIBBER_TOKEN','')
APP_PASSWORD=os.environ.get('APP_PASSWORD','')
FAMILY_PASSWORD=os.environ.get('FAMILY_PASSWORD','')
PORT=int(os.environ.get('PORT','8099'))
TZ=ZoneInfo(os.environ.get('TZ','Europe/Stockholm'))
lock=threading.RLock()
def load():
    try: return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError,ValueError): return {}
state={'rules':{},'managed':{},'logs':[],'enabled':False,'prices':[],'last_fetch':0,'error':'','last_check':0,'home_id':os.environ.get('TIBBER_HOME_ID','')}
state.update(load())
# Price cache is intentionally not trusted across a restart.
state['prices']=[]; state['last_fetch']=0

def save():
    obj={k:v for k,v in state.items() if k not in ('prices','last_fetch','error','last_check')}
    tmp=STATE_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)); tmp.replace(STATE_FILE)
def log(msg):
    state['logs']=[{'time':datetime.now(TZ).isoformat(timespec='seconds'),'message':msg}]+state['logs'][:99]
    save()
def request_json(url, token, payload=None, timeout=12, ua=False):
    headers={'Authorization':'Bearer '+token,'Accept':'application/json'}
    if payload is not None: headers['Content-Type']='application/json'
    if ua: headers['User-Agent']='TibberSmartControl/1.0 Python-stdlib/1.0'
    req=urllib.request.Request(url,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.load(r)
def ha_states():
    if not HA_TOKEN: raise ValueError('HA_TOKEN saknas')
    return request_json(HA_URL+'/api/states',HA_TOKEN)
def ha_switch(entity,on):
    if not entity.startswith('switch.'): raise ValueError('Endast switch.* tillåts')
    return request_json(HA_URL+'/api/services/switch/'+('turn_on' if on else 'turn_off'),HA_TOKEN,{'entity_id':entity})
QUERY='''{ viewer { homes { id appNickname currentSubscription { priceInfo(resolution: QUARTER_HOURLY) { today { total startsAt currency } tomorrow { total startsAt currency } } } } } }'''
def fetch_prices():
    if not TIBBER_TOKEN: raise ValueError('TIBBER_TOKEN saknas')
    res=request_json('https://api.tibber.com/v1-beta/gql',TIBBER_TOKEN,{'query':QUERY},ua=True)
    if res.get('errors'): raise ValueError(str(res['errors'][0].get('message','Tibber API-fel')))
    homes=res['data']['viewer']['homes']
    candidates=[h for h in homes if h.get('currentSubscription')]
    home=next((h for h in candidates if h['id']==state['home_id']),None)
    if home is None:
        if state['home_id']: raise ValueError('Valt Tibber-hem hittades inte')
        if len(candidates)!=1: raise ValueError('Ange TIBBER_HOME_ID för att välja bland flera hem')
        home=candidates[0]
    info=home['currentSubscription']['priceInfo']
    prices=sorted(({'time':p['startsAt'],'total':p['total'],'currency':p['currency']} for p in info['today']+info['tomorrow'] if p.get('total') is not None),key=lambda p:p['time'])
    if not prices: raise ValueError('Inga elpriser tillgängliga')
    with lock:
        state['prices']=prices; state['last_fetch']=time.time(); state['error']=''
    return prices
def current_price():
    now=datetime.now(timezone.utc)
    active=None
    for p in state['prices']:
        if datetime.fromisoformat(p['time'].replace('Z','+00:00'))<=now: active=p
        else: break
    if active is None: return None
    # Reject stale price periods (normally 15 minutes; tolerate 20).
    if (now-datetime.fromisoformat(active['time'].replace('Z','+00:00'))).total_seconds()>20*60:return None
    return active
def evaluate():
    with lock:
        state['last_check']=time.time()
        if not state['enabled'] or not HA_TOKEN: return
        p=current_price()
        if p is None:
            state['error']='Inget aktuellt kvartspris – inga styrningar utförs';return
        try: devices={x['entity_id']:x['state'] for x in ha_states() if x['entity_id'].startswith('switch.')}
        except Exception as e: state['error']='Home Assistant: '+str(e);return
        price=p['total']
        for entity,rule in list(state['rules'].items()):
            if not rule.get('enabled',False) or entity not in devices: continue
            # Managed state is persisted; never turn on a switch unless we previously turned it off.
            managed=state['managed'].get(entity)
            threshold=float(rule['threshold']); hysteresis=float(rule.get('hysteresis',0.10))
            cooldown=int(rule.get('cooldown',300))
            if managed and time.time()-managed.get('changed',0)<cooldown: continue
            try:
                if price>threshold and devices[entity]=='on' and not managed:
                    ha_switch(entity,False)
                    state['managed'][entity]={'changed':time.time()}; log(f'{entity}: AV vid {price:.3f} kr/kWh')
                elif managed and devices[entity]=='off' and price<=threshold-hysteresis:
                    ha_switch(entity,True)
                    state['managed'].pop(entity,None);log(f'{entity}: PÅ vid {price:.3f} kr/kWh')
                elif managed and devices[entity]=='on':
                    # Someone manually overrode the switch. Release ownership.
                    state['managed'].pop(entity,None);log(f'{entity}: manuell påslagning upptäckt; släpper kontroll')
            except Exception as e: state['error']=f'{entity}: {e}';log(state['error'])
        save()
def worker():
    while True:
        try:
            if TIBBER_TOKEN and (not state['prices'] or time.time()-state['last_fetch']>1800): fetch_prices()
            evaluate()
        except Exception as e:
            with lock: state['error']=str(e)
        time.sleep(60)
HTML='''<!doctype html><html lang="sv"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tibber Smart Control</title><style>
:root{font-family:system-ui,-apple-system,sans-serif;color-scheme:dark;background:#0a1020;color:#edf3ff}*{box-sizing:border-box}body{margin:0;padding:20px;max-width:1050px;margin:auto}h1{font-size:clamp(25px,5vw,38px);margin:6px 0}p,.muted{color:#9eaec8}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.card{background:#151e32;border:1px solid #2c3954;border-radius:18px;padding:20px;margin:16px 0}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.stats .card{margin:0}.big{font-size:30px;font-weight:750;margin-top:8px}button,input{font:inherit}button{border:0;border-radius:10px;padding:10px 15px;background:#6d80ff;color:white;cursor:pointer}button.secondary{background:#34415d}button.danger{background:#833f55}button:disabled{opacity:.5;cursor:not-allowed}input[type=number]{background:#0d1527;color:white;border:1px solid #43506a;border-radius:8px;padding:9px;width:100px}input[type=checkbox]{width:19px;height:19px;accent-color:#7b8bff}.device{display:flex;justify-content:space-between;gap:15px;align-items:center;padding:14px 0;border-top:1px solid #2a3650;flex-wrap:wrap}.device-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.pill{border-radius:99px;padding:5px 10px;background:#2c3953;font-size:12px}.ok{color:#77e0aa}.warn{color:#ffbf77}.error{color:#ff9da8}canvas{width:100%;height:210px}small{color:#9eaec8}#logs{max-height:220px;overflow:auto}#logs div{padding:7px 0;border-bottom:1px solid #29344b}label{display:flex;gap:7px;align-items:center}a{color:#a9b5ff}@media(max-width:600px){body{padding:12px}.card{padding:15px}.device-controls{width:100%}}</style>
<div class="top"><div><small>⚡ HEMMAAUTOMATION</small><h1>Tibber Smart Control</h1><p>Prisbaserad styrning via Home Assistant</p></div><label><input id="global" type="checkbox" onchange="toggleGlobal(this.checked)"> Automatik aktiv</label></div>
<div id="error" class="error"></div><div class="stats"><div class="card"><small>Elpris just nu</small><div class="big" id="price">–</div><small>kr/kWh, Tibber totalpris</small></div><div class="card"><small>Styrda enheter</small><div class="big" id="count">–</div></div><div class="card"><small>Nästa prisperiod</small><div class="big" id="next">–</div></div></div>
<div class="card"><h2>Elpriser</h2><canvas id="chart" width="900" height="210"></canvas><small>Idag och imorgon, när morgondagens priser finns. Priser inklusive Tibbers angivna skatter.</small></div>
<div class="card"><div class="top"><h2>Enheter och regler</h2><button class="secondary" onclick="refresh()">↻ Uppdatera</button></div><p>Aktivera en regel och välj prisgräns. Återstart sker först när priset är minst 10 öre under gränsen. Minst 5 minuter mellan appens växlingar.</p><div id="devices">Läser enheter…</div></div>
<div class="card"><h2>Händelser</h2><div id="logs"></div></div><small>Tips: lägg endast till enheter som tål att strömmen bryts. Appen styr bara switch-entiteter. Skydda åtkomsten med lösenord och HTTPS/VPN.</small>
<script>
let data=null;const $=id=>document.getElementById(id);const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,method='GET',body){const r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);return d}
async function toggleGlobal(enabled){try{await api('enabled','POST',{enabled});await refresh()}catch(e){alert(e.message);await refresh()}}
async function rule(entity,enabled){let row=document.querySelector('[data-entity="'+CSS.escape(entity)+'"]');let threshold=Number(row.querySelector('input[type=number]').value);if(!Number.isFinite(threshold)||threshold<0||threshold>20){alert('Välj pris mellan 0 och 20 kr');return}try{await api('rule','POST',{entity,enabled,threshold});await refresh()}catch(e){alert(e.message)}}
async function release(entity){if(!confirm('Släpp appens kontroll över '+entity+'? Enheten slås inte på automatiskt.'))return;await api('release','POST',{entity});await refresh()}
function draw(prices){let c=$('chart'),ctx=c.getContext('2d'),w=c.width,h=c.height;ctx.clearRect(0,0,w,h);if(!prices.length)return;let vals=prices.map(p=>p.total),max=Math.max(.1,...vals)*1.15,min=Math.min(0,...vals),bw=(w-60)/vals.length;ctx.strokeStyle='#40506b';ctx.beginPath();ctx.moveTo(35,10);ctx.lineTo(35,h-30);ctx.lineTo(w-10,h-30);ctx.stroke();prices.forEach((p,i)=>{let x=40+i*bw,y=h-30-(p.total-min)/(max-min)*(h-48);ctx.fillStyle=p.total>.8?'#f3a76b':'#7a8fff';ctx.fillRect(x,y,Math.max(1,bw-1),h-30-y)});ctx.fillStyle='#aebcdb';ctx.font='13px system-ui';ctx.fillText(max.toFixed(2)+' kr',3,16);ctx.fillText('0',20,h-34);ctx.fillText('Nu',40,h-8);ctx.fillText('→ Tid',w-60,h-8)}
async function refresh(){try{data=await api('status');$('global').checked=data.enabled;$('price').textContent=data.current?data.current.total.toFixed(3).replace('.',','):'–';$('count').textContent=Object.keys(data.rules).filter(x=>data.rules[x].enabled).length;$('next').textContent=data.next?new Date(data.next.time).toLocaleTimeString('sv-SE',{hour:'2-digit',minute:'2-digit'}):'–';$('error').textContent=data.error||'';draw(data.prices);$('devices').innerHTML=data.devices.map(d=>{let r=data.rules[d.entity_id]||{},m=data.managed[d.entity_id];return `<div class="device" data-entity="${esc(d.entity_id)}"><div><strong>${esc(d.name)}</strong><br><small>${esc(d.entity_id)}</small> <span class="pill">${esc(d.state)}</span> ${m?'<span class="warn">Stoppad av appen</span>':''}</div><div class="device-controls"><label>Maxpris <input type="number" step="0.05" min="0" max="20" value="${r.threshold??0.8}"> kr</label><label><input type="checkbox" ${r.enabled?'checked':''} onchange="rule('${esc(d.entity_id)}',this.checked)"> Styr</label><button class="secondary" onclick="rule('${esc(d.entity_id)}',document.querySelector('[data-entity=&quot;${esc(d.entity_id)}&quot;] input[type=checkbox]').checked)">Spara</button>${m?`<button class="danger" onclick="release('${esc(d.entity_id)}')">Släpp</button>`:''}</div></div>`}).join('')||'<p>Inga switch-enheter hittades. Kontrollera Home Assistant-anslutningen.</p>';$('logs').innerHTML=data.logs.map(l=>`<div><small>${esc(l.time)}</small> ${esc(l.message)}</div>`).join('')||'<p>Inga händelser ännu.</p>'}catch(e){$('error').textContent='Anslutningsfel: '+e.message}}
refresh();setInterval(refresh,30000);
</script></html>'''
class Handler(BaseHTTPRequestHandler):
    def auth(self):
        value=self.headers.get('Authorization','')
        admin='Basic '+base64.b64encode(('admin:'+APP_PASSWORD).encode()).decode()
        family='Basic '+base64.b64encode(('family:'+FAMILY_PASSWORD).encode()).decode()
        if APP_PASSWORD and secrets.compare_digest(value,admin): return 'admin'
        if FAMILY_PASSWORD and secrets.compare_digest(value,family): return 'family'
        return None
    def respond(self,status,data,typ='application/json'):
        raw=(json.dumps(data,ensure_ascii=False) if typ=='application/json' else data).encode()
        self.send_response(status);self.send_header('Content-Type',typ+'; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self' 'unsafe-inline'; connect-src 'self'");self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def handle_request(self):
        role=self.auth()
        if not role:
            self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Tibber Smart Control"');self.end_headers();return
        try:
            if self.command=='GET' and self.path=='/':
                page=HTML if role=='admin' else HTML.replace('<label><input id="global" type="checkbox" onchange="toggleGlobal(this.checked)"> Automatik aktiv</label>','').replace('<div class="card"><div class="top"><h2>Enheter och regler</h2>', '<div class="card"><div class="top"><h2>Enheter och regler</h2>')
                if role=='family':
                    page=page.replace('refresh();setInterval(refresh,30000);', '''const oldRefresh=refresh;refresh=async function(){try{const d=await api('status');$('price').textContent=d.current?d.current.total.toFixed(3).replace('.',','):'–';$('count').textContent=d.device_count;$('next').textContent=d.next?new Date(d.next.time).toLocaleTimeString('sv-SE',{hour:'2-digit',minute:'2-digit'}):'–';$('error').textContent=d.error||'';draw(d.prices);$('devices').innerHTML='<p>Styrda enheter: '+d.device_count+'</p>';$('logs').innerHTML='';}catch(e){$('error').textContent=e.message}};refresh();setInterval(refresh,30000);''')
                return self.respond(200,page,'text/html')
            if self.command=='GET' and self.path=='/api/status':
                with lock:
                    try:
                        devices=[{'entity_id':s['entity_id'],'name':s.get('attributes',{}).get('friendly_name',s['entity_id']),'state':s['state']} for s in ha_states() if s['entity_id'].startswith('switch.')]
                    except Exception as e: devices=[];state['error']='Home Assistant: '+str(e)
                    p=current_price();now=datetime.now(timezone.utc)
                    upcoming=next((x for x in state['prices'] if datetime.fromisoformat(x['time'].replace('Z','+00:00'))>now),None)
                    if role=='family': return self.respond(200,{'current':p,'next':upcoming,'prices':state['prices'],'device_count':sum(1 for r in state['rules'].values() if r.get('enabled')),'error':state['error']})
                    return self.respond(200,{'devices':devices,'rules':state['rules'],'managed':state['managed'],'enabled':state['enabled'],'current':p,'next':upcoming,'prices':state['prices'],'error':state['error'],'logs':state['logs'][:30]})
            if self.command=='POST' and role!='admin': return self.respond(403,{'error':'Endast administratör'})
            if self.command=='POST':
                n=int(self.headers.get('Content-Length','0'))
                if n>4096: return self.respond(413,{'error':'För stor begäran'})
                body=json.loads(self.rfile.read(n) or b'{}')
                with lock:
                    if self.path=='/api/enabled':
                        state['enabled']=body.get('enabled') is True;log('Automatik '+('aktiverad' if state['enabled'] else 'pausad'));return self.respond(200,{'ok':True})
                    if self.path=='/api/rule':
                        e=body.get('entity',''); t=body.get('threshold')
                        if not isinstance(e,str) or not e.startswith('switch.') or not isinstance(t,(int,float)) or isinstance(t,bool) or not 0<=t<=20: return self.respond(400,{'error':'Ogiltig regel'})
                        available={s['entity_id'] for s in ha_states() if s['entity_id'].startswith('switch.')}
                        if e not in available: return self.respond(400,{'error':'Enheten finns inte i Home Assistant'})
                        state['rules'][e]={'threshold':float(t),'hysteresis':0.10,'cooldown':300,'enabled':body.get('enabled') is True};save();return self.respond(200,{'ok':True})
                    if self.path=='/api/release':
                        e=body.get('entity','');state['managed'].pop(e,None);log(f'{e}: kontroll släppt manuellt');return self.respond(200,{'ok':True})
            return self.respond(404,{'error':'Hittades inte'})
        except Exception as e: return self.respond(500,{'error':str(e)})
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
if __name__=='__main__':
    if not APP_PASSWORD: raise SystemExit('APP_PASSWORD måste anges i miljövariablerna')
    if not HA_TOKEN or not TIBBER_TOKEN: print('VARNING: HA_TOKEN eller TIBBER_TOKEN saknas; anslutningar fungerar inte ännu',flush=True)
    threading.Thread(target=worker,daemon=True).start()
    print(f'Tibber Smart Control på port {PORT}',flush=True)
    ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()

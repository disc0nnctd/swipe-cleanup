#!/usr/bin/env python3
"""Swipe-to-sort photo/video triage. Decisions -> decisions.json (relpath -> photos|drive|local|delete)."""
import json,os,sys,hashlib,time,mimetypes,threading
LOCK=threading.RLock()   # requests run in threads; all state changes + saves go through this
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from PIL import Image,ImageOps
import phonesync,subprocess
DRIVE_PY=os.path.expanduser(os.environ.get('DRIVE_PY','~/.venv/bin/python'))
DRIVE_TOOL=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),'tools','drive_remove.py')
djob=dict(state='idle')   # Drive removal runs in the background: plan / apply / undo / empty
def drive_run(kind,*args):
    djob.clear();djob.update(state='running',kind=kind,started=int(time.time()))
    def go():
        try:
            r=subprocess.run([DRIVE_PY,DRIVE_TOOL,kind,*args],capture_output=True,text=True,timeout=7200)
            out=r.stdout.strip().splitlines()
            djob.update(state='done' if r.returncode==0 else 'error',result=json.loads(out[-1]) if r.returncode==0 and out else None,
                error=None if r.returncode==0 else (r.stderr.strip().splitlines() or ['failed'])[-1][:300])
        except Exception as e:djob.update(state='error',error=str(e)[:300])
    threading.Thread(target=go,daemon=True).start()
ROOT=os.path.realpath(sys.argv[1] if len(sys.argv)>1 else '/path/to/storage/gdrive-backup')
PORT=int(sys.argv[2]) if len(sys.argv)>2 else 8091
HERE=os.path.dirname(os.path.abspath(__file__));DEC=os.path.join(HERE,'decisions.json');CACHE=os.path.join(HERE,'thumbs')
os.makedirs(CACHE,exist_ok=True)
IMG=('.jpg','.jpeg','.png','.webp','.gif','.bmp')
VID=('.mp4','.webm','.mov','.m4v')
def scan():
    out=[]
    for d,dirs,fs in os.walk(ROOT):
        dirs[:]=[x for x in dirs if x!='_sorted']
        for f in fs:
            fl=f.lower()
            if fl.endswith(IMG+VID):
                p=os.path.join(d,f);st=os.stat(p);rel=os.path.relpath(p,ROOT)
                out.append(dict(p=rel,folder=os.path.dirname(rel) or '(top level)',name=f,size=st.st_size,
                    mtime=int(st.st_mtime),kind='video' if fl.endswith(VID) else 'image'))
    return sorted(out,key=lambda x:(x['folder'],x['name']))
decs=json.load(open(DEC)) if os.path.exists(DEC) else {}
HIST=os.path.join(HERE,'history.json')
hist=json.load(open(HIST)) if os.path.exists(HIST) else []   # [{p,prev,label,at}] newest last
def _atomic(path,obj,**kw):
    tmp=f'{path}.{threading.get_ident()}.tmp'
    with open(tmp,'w') as f:json.dump(obj,f,**kw)
    os.replace(tmp,path)
def save():
    _atomic(DEC,decs,indent=0);_atomic(HIST,hist[-5000:])
def safe(rel):
    p=os.path.realpath(os.path.join(ROOT,rel))
    return p if p.startswith(ROOT+os.sep) and os.path.isfile(p) else None
class H(BaseHTTPRequestHandler):
    def log_message(s,*a):pass
    def send(s,code,body,ct,extra=()):
        s.send_response(code);s.send_header('Content-Type',ct);s.send_header('Content-Length',str(len(body)))
        for k,v in extra:s.send_header(k,v)
        s.end_headers();s.wfile.write(body)
    def do_GET(s):
        u=urlparse(s.path);q=parse_qs(u.query)
        if u.path=='/':return s.send(200,open(os.path.join(HERE,'index.html'),'rb').read(),'text/html; charset=utf-8',[('Cache-Control','no-store')])
        if u.path=='/list':
            items=scan()
            for x in items:x['label']=decs.get(x['p'])
            return s.send(200,json.dumps(dict(items=items,undo=len(hist))).encode(),'application/json')
        if u.path in('/img','/thumb'):
            p=safe(q.get('p',[''])[0])
            if not p:return s.send(404,b'','text/plain')
            px=1400 if u.path=='/img' else 320
            c=os.path.join(CACHE,hashlib.md5(p.encode()).hexdigest()+f'_{px}.jpg')
            if not os.path.exists(c):
                try:
                    im=ImageOps.exif_transpose(Image.open(p));im.thumbnail((px,px))
                    im.convert('RGB').save(c,quality=82)
                except Exception:return s.send(500,b'','text/plain')
            return s.send(200,open(c,'rb').read(),'image/jpeg',[('Cache-Control','max-age=86400')])
        if u.path=='/sync/status':
            try:return s.send(200,json.dumps(phonesync.status(decs,ROOT)).encode(),'application/json')
            except Exception as e:return s.send(200,json.dumps(dict(error=str(e)[:200])).encode(),'application/json')
        if u.path=='/drive/job':return s.send(200,json.dumps(dict(djob,has_token=os.path.exists(os.path.expanduser(os.environ.get('DRIVE_TOKEN_RW','~/.config/google-storage-cleanup/drive_token.json'))))).encode(),'application/json')
        if u.path=='/sync/qr.svg':
            return s.send(200,phonesync.qr_svg(phonesync.st('/rest/system/status')['myID']),'image/svg+xml')
        if u.path=='/meta':
            p=safe(q.get('p',[''])[0]);info={}
            if p and p.lower().endswith(IMG):
                try:
                    im=Image.open(p);info['dims']=f'{im.size[0]}×{im.size[1]}'
                    ex=im.getexif();dt=ex.get(36867) or ex.get(306)
                    if not dt:
                        dt=im.getexif().get_ifd(0x8769).get(36867)
                    if dt:info['taken']=str(dt)[:10].replace(':','-')
                    info['camera']=(ex.get(272) or '').strip()
                except Exception:pass
            return s.send(200,json.dumps(info).encode(),'application/json')
        if u.path=='/video':
            p=safe(q.get('p',[''])[0])
            if not p:return s.send(404,b'','text/plain')
            size=os.path.getsize(p);ct=mimetypes.guess_type(p)[0] or 'video/mp4'
            rng=s.headers.get('Range')
            start,end=0,size-1
            if rng and rng.startswith('bytes='):
                a,_,b=rng[6:].partition('-');start=int(a or 0);end=min(int(b) if b else size-1,size-1)
            with open(p,'rb') as f:f.seek(start);body=f.read(end-start+1)
            s.send_response(206 if rng else 200);s.send_header('Content-Type',ct);s.send_header('Accept-Ranges','bytes')
            s.send_header('Content-Length',str(len(body)))
            if rng:s.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            s.end_headers();s.wfile.write(body);return
        s.send(404,b'','text/plain')
    def do_POST(s):
        with LOCK:return s._post()
    def _post(s):
        if s.path=='/set':
            d=json.loads(s.rfile.read(int(s.headers['Content-Length'])))
            new=d['label'] if d['label'] in ('photos','drive','local','delete') else None
            prev=decs.get(d['p'])
            if new==prev:return s.send(200,b'{}','application/json')
            hist.append(dict(p=d['p'],prev=prev,label=new,at=int(time.time())))
            if new:decs[d['p']]=new
            else:decs.pop(d['p'],None)
            save();return s.send(200,json.dumps(dict(undo=len(hist))).encode(),'application/json')
        if s.path in('/sync/apply','/sync/accept'):
            n=int(s.headers.get('Content-Length') or 0);d=json.loads(s.rfile.read(n) or b'{}')
            try:r=phonesync.apply(decs,ROOT) if s.path=='/sync/apply' else phonesync.accept(d['id'])
            except Exception as e:r=dict(ok=False,error=str(e)[:300])
            return s.send(200,json.dumps(r).encode(),'application/json')
        if s.path.startswith('/drive/'):
            n=int(s.headers.get('Content-Length') or 0);d=json.loads(s.rfile.read(n) or b'{}')
            if djob.get('state')=='running':return s.send(200,b'{"ok":false,"error":"busy"}','application/json')
            labels=','.join(l for l in d.get('labels',[]) if l in('delete','local','photos')) or 'delete,local'
            k=s.path[7:]
            if k=='plan':drive_run('plan',ROOT,DEC,labels,'--json')
            elif k=='apply' and d.get('confirm')=='yes':drive_run('apply',ROOT,DEC,labels)
            elif k=='undo':drive_run('undo',ROOT)
            elif k=='empty' and d.get('confirm')=='yes':drive_run('empty')
            else:return s.send(400,b'{"ok":false}','application/json')
            return s.send(200,b'{"ok":true}','application/json')
        if s.path=='/bulk':
            # whole folder -> one label, as ONE undo step. Only unsorted items unless all=True.
            d=json.loads(s.rfile.read(int(s.headers['Content-Length'])))
            lab=d['label'];f=d['folder'];sub=d.get('subfolders');everything=d.get('all')
            if lab not in ('photos','drive','local','delete'):return s.send(400,b'{}','application/json')
            inf=lambda x:x['folder']==f or (sub and x['folder'].startswith(f+'/'))
            items=[x['p'] for x in scan() if inf(x) and (everything or x['p'] not in decs) and decs.get(x['p'])!=lab]
            if not items:return s.send(200,json.dumps(dict(ok=True,n=0)).encode(),'application/json')
            hist.append(dict(items=[dict(p=p,prev=decs.get(p)) for p in items],label=lab,folder=f,at=int(time.time())))
            for p in items:decs[p]=lab
            save();return s.send(200,json.dumps(dict(ok=True,n=len(items),undo=len(hist))).encode(),'application/json')
        if s.path=='/undo':
            if not hist:return s.send(200,b'{"ok":false}','application/json')
            h=hist.pop()
            if 'items' in h:
                for it in h['items']:
                    if it['prev']:decs[it['p']]=it['prev']
                    else:decs.pop(it['p'],None)
                save();return s.send(200,json.dumps(dict(ok=True,bulk=len(h['items']),folder=h['folder'],label=h['label'],left=len(hist))).encode(),'application/json')
            if h['prev']:decs[h['p']]=h['prev']
            else:decs.pop(h['p'],None)
            save();return s.send(200,json.dumps(dict(ok=True,p=h['p'],prev=h['prev'],label=h['label'],left=len(hist))).encode(),'application/json')
        s.send(404,b'','text/plain')
ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()

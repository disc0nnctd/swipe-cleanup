"""Phone sync: the 'photos' bucket is mirrored as COPIES into SYNC (the expansion drive is exFAT: no hard/sym links),
and Syncthing sends SYNC (send-only) to the phone, where Google Photos backs it up.
Only an explicit button press (/sync/apply) changes SYNC, so a stray swipe never reaches Google."""
import os,json,re,io,shutil,urllib.request
import qrcode,qrcode.image.svg
SYNC=os.environ.get('PHONE_SYNC_DIR','/path/to/storage/google-photos-sync')
FOLDER='google-photos'
PHONE_ADDR=os.environ.get('PHONE_ADDR','dynamic')      # e.g. tcp://<phone tailnet ip>:22000
PHONE_NAME=os.environ.get('PHONE_NAME','Phone')
CFG=os.path.expanduser(os.environ.get('SYNCTHING_CONFIG','~/.local/state/syncthing/config.xml'))
def _key():return re.search(r'<apikey>([^<]+)',open(CFG).read()).group(1)
def st(path,method='GET',body=None):
    req=urllib.request.Request('http://127.0.0.1:8384'+path,method=method,headers={'X-API-Key':_key(),'Content-Type':'application/json'},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req,timeout=10) as r:
        t=r.read();return json.loads(t) if t.strip() else {}

def wanted(decs):return {p for p,l in decs.items() if l=='photos'}
def current():
    out=set()
    for d,_,fs in os.walk(SYNC):
        if '.stfolder' in d or '.stversions' in d:continue
        for f in fs:
            if f.startswith('.st') or f.endswith('.part'):continue
            out.add(os.path.relpath(os.path.join(d,f),SYNC))
    return out
def plan(decs,root):
    w={p for p in wanted(decs) if os.path.exists(os.path.join(root,p))};c=current()
    return sorted(w-c),sorted(c-w)
def apply(decs,root):
    add,rem=plan(decs,root)
    done=0;removed=0;errors=[]
    for p in add:
        t=os.path.join(SYNC,p)
        try:
            os.makedirs(os.path.dirname(t),exist_ok=True)
            shutil.copy2(os.path.join(root,p),t+'.part');os.replace(t+'.part',t);done+=1   # copy2 keeps the date
        except Exception as e:errors.append(f'{p}: {e}')
    for p in rem:
        try:os.remove(os.path.join(SYNC,p));removed+=1
        except Exception as e:errors.append(f'{p}: {e}')
    for d,dirs,fs in sorted(os.walk(SYNC,topdown=False)):
        if d!=SYNC and not os.listdir(d) and '.stfolder' not in d:os.rmdir(d)   # never remove Syncthing's marker
    os.makedirs(os.path.join(SYNC,'.stfolder'),exist_ok=True)
    try:st(f'/rest/db/scan?folder={FOLDER}','POST')
    except Exception:pass
    return dict(added=done,removed=removed,failed=len(errors),errors=errors[:5])

def phone_id():
    for d in st('/rest/config/devices'):
        if d['name']==PHONE_NAME:return d['deviceID']
    return None
def status(decs,root):
    add,rem=plan(decs,root)
    me=st('/rest/system/status')['myID']
    out=dict(myID=me,pending_add=len(add),pending_remove=len(rem),in_sync_folder=len(current()),phone=None,pending_devices=[])
    pend=st('/rest/cluster/pending/devices')
    out['pending_devices']=[dict(id=k,name=v.get('name',''),address=v.get('address','')) for k,v in pend.items()]
    pid=phone_id()
    if pid:
        con=st('/rest/system/connections')['connections'].get(pid,{})
        comp=st(f'/rest/db/completion?folder={FOLDER}&device={pid}')
        fpend=st('/rest/cluster/pending/folders')
        out['phone']=dict(id=pid,connected=con.get('connected',False),address=con.get('address',''),
            completion=round(comp.get('completion',0),1),need_files=comp.get('needItems',0),need_bytes=comp.get('needBytes',0),
            remote_state=comp.get('remoteState',''))
    return out
def accept(dev_id):
    devs=st('/rest/config/devices')
    if not any(d['deviceID']==dev_id for d in devs):
        st('/rest/config/devices','POST',dict(deviceID=dev_id,name=PHONE_NAME,addresses=list(dict.fromkeys([PHONE_ADDR,'dynamic'])),autoAcceptFolders=False))
    folders={f['id']:f for f in st('/rest/config/folders')}
    if FOLDER in folders:
        f=folders[FOLDER]
        if not any(d['deviceID']==dev_id for d in f['devices']):
            f['devices'].append(dict(deviceID=dev_id));st(f'/rest/config/folders/{FOLDER}','PUT',f)
    else:
        os.makedirs(SYNC,exist_ok=True)
        st('/rest/config/folders','POST',dict(id=FOLDER,label='Google Photos (from Drive)',path=SYNC,type='sendonly',
            fsWatcherEnabled=True,rescanIntervalS=3600,devices=[dict(deviceID=dev_id)]))
    return dict(ok=True)
def qr_svg(text):
    img=qrcode.make(text,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=2)
    b=io.BytesIO();img.save(b);return b.getvalue()

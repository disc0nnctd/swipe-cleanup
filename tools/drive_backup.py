#!/usr/bin/env python3
"""Back up your own Google Drive files to a local disk, keeping the folder layout.

  drive_backup.py report               storage quota + biggest files + duplicates
  drive_backup.py backup DEST          download new/changed files, verify md5, keep Drive dates

Incremental: a file is skipped when DEST already has it with the same md5 (tracked in DEST/.drive-backup.json).
Google Docs/Sheets/Slides (no md5, no binary) are skipped; export them with Takeout if needed.
Token: DRIVE_TOKEN env (authorized-user JSON with drive.readonly or drive scope).
"""
import collections,datetime,hashlib,json,os,re,sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

TOKEN=os.path.expanduser(os.environ.get('DRIVE_TOKEN','~/.config/google-storage-cleanup/drive_token.json'))
FIELDS='nextPageToken,files(id,name,mimeType,size,quotaBytesUsed,modifiedTime,parents,md5Checksum)'

def drive():
    c=Credentials.from_authorized_user_file(TOKEN)
    if not c.valid:c.refresh(Request())
    return build('drive','v3',credentials=c,cache_discovery=False)

def list_all(dr,q):
    out,tok=[],None
    while True:
        r=dr.files().list(q=q,pageSize=1000,pageToken=tok,fields=FIELDS).execute()
        out+=r['files'];tok=r.get('nextPageToken')
        if not tok:return out

def folder_paths(dr):
    folders={f['id']:f for f in list_all(dr,"mimeType='application/vnd.google-apps.folder' and trashed=false")}
    def path(pid):
        parts=[]
        while pid in folders:
            parts.append(clean(folders[pid]['name']));pid=(folders[pid].get('parents') or [None])[0]
        return '/'.join(reversed(parts))
    return path

def clean(name):return re.sub(r'[/\0]','_',name).strip() or '_'
def mb(b):return f'{int(b)/1e6:,.0f} MB'

def report():
    dr=drive()
    q=dr.about().get(fields='storageQuota,user(emailAddress)').execute()['storageQuota']
    print(f"Account quota: {mb(q['usage'])} used of {mb(q.get('limit',0))}  (Drive {mb(q['usageInDrive'])}, trash {mb(q['usageInDriveTrash'])})")
    files=list_all(dr,"trashed=false and 'me' in owners")
    size=lambda f:int(f.get('quotaBytesUsed',0))
    print(f'{len(files)} files you own, {mb(sum(map(size,files)))}')
    kinds=collections.Counter();n=collections.Counter()
    for f in files:k=f['mimeType'].split('/')[-1][:30];kinds[k]+=size(f);n[k]+=1
    print('\nBy type:');[print(f'  {mb(b):>10} {n[k]:6} {k}') for k,b in kinds.most_common(10)]
    print('\nBiggest:');[print(f"  {mb(size(f)):>10} {f['modifiedTime'][:10]} {f['name'][:70]}") for f in sorted(files,key=size,reverse=True)[:20]]
    d=collections.defaultdict(list)
    for f in files:
        if f.get('md5Checksum'):d[f['md5Checksum']].append(f)
    dup=[v for v in d.values() if len(v)>1]
    print(f'\nExact duplicates: {len(dup)} groups wasting {mb(sum(size(v[0])*(len(v)-1) for v in dup))}')

def backup(dest):
    dr=drive();path=folder_paths(dr)
    state_p=os.path.join(dest,'.drive-backup.json')
    state=json.load(open(state_p)) if os.path.exists(state_p) else {}   # drive id -> {path, md5}
    files=[f for f in list_all(dr,"trashed=false and 'me' in owners") if f.get('md5Checksum')]
    todo=[f for f in files if state.get(f['id'],{}).get('md5')!=f['md5Checksum'] or not os.path.exists(state[f['id']]['path'])]
    print(f'{len(files)} files, {len(todo)} new or changed',flush=True)
    failed=[]
    try:_run(dr,path,dest,todo,state,state_p,failed)
    finally:json.dump(state,open(state_p,'w'))   # keep progress even on Ctrl-C
    for n,e in failed:print('FAILED',n,'-',e)

def _run(dr,path,dest,todo,state,state_p,failed):
    for i,f in enumerate(todo,1):
        d=os.path.join(dest,path((f.get('parents') or [None])[0]));os.makedirs(d,exist_ok=True)
        p=state.get(f['id'],{}).get('path') or os.path.join(d,clean(f['name']))
        if f['id'] not in state and os.path.exists(p):p=os.path.join(d,f['id'][:6]+'_'+clean(f['name']))
        try:
            with open(p+'.part','wb') as fh:
                dl=MediaIoBaseDownload(fh,dr.files().get_media(fileId=f['id']),chunksize=32<<20);done=False
                while not done:_,done=dl.next_chunk()
            h=hashlib.md5()
            with open(p+'.part','rb') as fh:
                for chunk in iter(lambda:fh.read(1<<20),b''):h.update(chunk)
            if h.hexdigest()!=f['md5Checksum']:raise ValueError('md5 mismatch')
            os.replace(p+'.part',p)
            t=datetime.datetime.fromisoformat(f['modifiedTime'].replace('Z','+00:00')).timestamp();os.utime(p,(t,t))
            state[f['id']]=dict(path=p,md5=f['md5Checksum'])
        except Exception as e:
            failed.append((f['name'],str(e)[:120]))
            if os.path.exists(p+'.part'):os.remove(p+'.part')
        if i%10==0 or i==len(todo):
            json.dump(state,open(state_p,'w'));print(f'{i}/{len(todo)} done, {len(failed)} failed',flush=True)

if __name__=='__main__':
    if len(sys.argv)>=2 and sys.argv[1]=='report':report()
    elif len(sys.argv)>=3 and sys.argv[1]=='backup':backup(sys.argv[2])
    else:print(__doc__)

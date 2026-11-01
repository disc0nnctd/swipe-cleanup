#!/usr/bin/env python3
"""Move sorted files out of Google Drive (to Drive's Trash) once a verified local copy exists.

  drive_remove.py plan  ROOT DECISIONS [labels]     what would be trashed (default labels: delete,local)
  drive_remove.py apply ROOT DECISIONS [labels]     trash them (restorable from Drive Trash for 30 days)
  drive_remove.py undo  ROOT                        restore everything trashed by the last apply
  drive_remove.py empty                             empty Drive Trash (permanent; trash still counts toward quota)

A Drive file is only trashed when ROOT/<its Drive path> exists with the same md5 as Drive.
For the 'photos' label the phone-sync copy must also exist with the same size.
Log of trashed ids: ROOT/.drive-removed.json
"""
import hashlib,json,os,sys
sys.path.insert(0,os.path.dirname(__file__))
from drive_backup import drive,list_all,folder_paths,clean

SYNC=os.environ.get('PHONE_SYNC_DIR','/path/to/storage/google-photos-sync')

def md5(p):
    h=hashlib.md5()
    with open(p,'rb') as f:
        for c in iter(lambda:f.read(1<<20),b''):h.update(c)
    return h.hexdigest()

def plan(root,decs,labels,dr=None):
    dr=dr or drive();path=folder_paths(dr)
    want={p:l for p,l in decs.items() if l in labels}
    byrel={}
    for f in list_all(dr,"trashed=false and 'me' in owners"):
        if not f.get('md5Checksum'):continue
        d=path((f.get('parents') or [None])[0])
        for name in (clean(f['name']),f['id'][:6]+'_'+clean(f['name'])):   # backup prefixes ids on name clashes
            byrel.setdefault(os.path.normpath(os.path.join(d,name)),[]).append(f)
    ok,skip=[],[]
    for rel,lab in sorted(want.items()):
        local=os.path.join(root,rel)
        if not os.path.exists(local):skip.append((rel,'no local copy'));continue
        h=md5(local);cands=[f for f in byrel.get(os.path.normpath(rel),[]) if f['md5Checksum']==h]
        if not cands:skip.append((rel,'not on Drive (or changed)'));continue
        if lab=='photos':
            s=os.path.join(SYNC,rel)
            if not os.path.exists(s) or os.path.getsize(s)!=os.path.getsize(local):skip.append((rel,'not in phone sync'));continue
        for f in cands:ok.append(dict(id=f['id'],rel=rel,label=lab,size=int(f.get('quotaBytesUsed') or f.get('size') or 0)))
    return ok,skip

def apply(root,decs,labels):
    dr=drive();ok,skip=plan(root,decs,labels,dr)
    logp=os.path.join(root,'.drive-removed.json')
    log=json.load(open(logp)) if os.path.exists(logp) else []
    batch=dict(labels=sorted(labels),ids=[]);log.append(batch);done=0;failed=[]
    try:
        for i,f in enumerate(ok,1):
            try:dr.files().update(fileId=f['id'],body={'trashed':True}).execute();batch['ids'].append(f['id']);done+=f['size']
            except Exception as e:failed.append((f['rel'],str(e)[:120]))
            if i%50==0:print(f'{i}/{len(ok)}',flush=True)
    finally:json.dump(log,open(logp,'w'))
    return dict(trashed=len(batch['ids']),bytes=done,skipped=len(skip),failed=failed)

def undo(root):
    logp=os.path.join(root,'.drive-removed.json');log=json.load(open(logp))
    dr=drive();b=log.pop();n=0
    for i in b['ids']:dr.files().update(fileId=i,body={'trashed':False}).execute();n+=1
    json.dump(log,open(logp,'w'));return dict(restored=n)

def empty():
    dr=drive();dr.files().emptyTrash().execute();return dict(emptied=True)

if __name__=='__main__':
    a=sys.argv[1:]
    if not a:print(__doc__);sys.exit()
    if a[0]=='empty':print(json.dumps(empty()));sys.exit()
    if a[0]=='undo':print(json.dumps(undo(a[1])));sys.exit()
    root,decs=a[1],json.load(open(a[2]));labels=set((a[3] if len(a)>3 and not a[3].startswith('-') else 'delete,local').split(','))
    if a[0]=='plan' and '--json' in a:
        ok,skip=plan(root,decs,labels);by={}
        for f in ok:b=by.setdefault(f['label'],dict(n=0,bytes=0));b['n']+=1;b['bytes']+=f['size']
        print(json.dumps(dict(by=by,skipped=len(skip),skip=skip[:20])))
    elif a[0]=='plan':
        ok,skip=plan(root,decs,labels)
        import collections;c=collections.Counter(f['label'] for f in ok);b=collections.Counter()
        for f in ok:b[f['label']]+=f['size']
        print({k:(c[k],f'{b[k]/1e6:,.0f} MB') for k in c});print('skipped',len(skip),collections.Counter(r for _,r in skip))
        for s in skip[:15]:print('  ',s)
    elif a[0]=='apply':print(json.dumps(apply(root,decs,labels)))

#!/usr/bin/env python3
"""Mail Swipe: triage Gmail one sender at a time (trash all / trash old / keep).

Trash is Gmail's own Trash (auto-emptied after 30 days, restorable). Every action is logged in
index.db:action with the exact message ids, so Undo / Restore untrashes precisely those.
Never touches: starred mail, mail you sent, mail from your own address.
"""
import json,os,re,sys,time,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from gmailapi import service,db,tmpl,notekey,TYPE_KEEP
PORT=int(sys.argv[1]) if len(sys.argv)>1 else 8092
HERE=os.path.dirname(os.path.abspath(__file__))
LOG=os.environ.get('MAILSWIPE_LOG') or os.path.join(HERE,'index.log')
YEAR=365*86400
DAY=86400
DELETABLE=('txn_alert','otp_security','marketing','notice')
_types={'n':-1,'map':{}}
def types():
    c=db();n=tuple(c.execute('select count(*),max(at) from mail_type').fetchone())   # changes on new rows AND overrides
    if n!=_types['n']:_types.update(n=n,map={(r['sender'],r['tmpl']):r['type'] for r in c.execute('select sender,tmpl,type from mail_type')})
    return _types['map']
def mtype(r,T):return T.get((r['sender'],tmpl(r['subject'])))
def past_keep(r,t):
    k=TYPE_KEEP.get(t);return k is not None and r['date']<time.time()-k*DAY
CATS={'promotions':'CATEGORY_PROMOTIONS','social':'CATEGORY_SOCIAL','updates':'CATEGORY_UPDATES',
      'forums':'CATEGORY_FORUMS','primary':'CATEGORY_PERSONAL'}
# Money, tax, identity, health: flagged so trashing them takes an extra confirmation.
PROTECT=re.compile(r'bank|icici|hdfc|sbi\b|axis|kotak|yes ?bank|idfc|indusind|rbl|nsdl|cdsl|zerodha|kite|groww|'
    r'dhan|upstox|angel|paytm money|incometax|income tax|\bitr\b|form ?16|\btds\b|traces|gst|epfo|\bpf\b|uan|'
    r'insurance|\blic\b|policy|bajaj|hdfc ?life|star ?health|demat|cams|kfin|mutual fund|\bsip\b|nps|'
    r'aadhaar|\bpan\b|passport|uidai|digilocker|statement|invoice|receipt|salary|payslip|loan|emi|'
    r'credit card|tax|hospital|medical|lab report|apollo|practo',re.I)
lock=threading.Lock()
_local=threading.local()
def svc():
    # one Gmail client per thread: the underlying httplib2 connection is NOT thread-safe, and sharing it
    # between request threads and the queue worker crossed responses (e.g. getProfile returning a str)
    if getattr(_local,'svc',None) is None:_local.svc=service()
    return _local.svc

_profile=None
def profile():
    global _profile
    if _profile is None:
        p=svc().users().getProfile(userId='me').execute()
        if not isinstance(p,dict) or 'emailAddress' not in p:raise RuntimeError(f'bad Gmail profile response: {str(p)[:80]}')
        _profile=p
    return _profile
def me():return profile()['emailAddress'].lower()

def eligible(r):
    """Messages we are allowed to trash."""
    labs=r['labels'].split(',')
    return r['state']=='live' and 'STARRED' not in labs and 'SENT' not in labs and r['sender']!=me()

_cache={};_ver=[0]
def bump():_ver[0]+=1;_cache.clear()
def senders(cat):
    aiv=db().execute('select count(*),max(at) from sender_ai').fetchone()   # new AI results also refresh the list
    k=(cat,aiv[0],aiv[1])
    if k in _cache:return _cache[k]
    _cache[k]=_senders(cat);return _cache[k]
def _senders(cat):
    con=db();notes={r['sender']:r['decision'] for r in con.execute('select * from sender_note')}
    ai={r['sender']:r for r in con.execute('select * from sender_ai')}
    uns={r['sender']:dict(r) for r in con.execute('select * from unsubs')}
    gone={r[0]:(r[1],r[2]) for r in con.execute("select sender,count(*),sum(size) from msg where state='trashed' group by sender")}
    via={}
    for r in con.execute("select sender,kind,coalesce(notekey,sender) nk,at from action where kind like 'trash%' order by id"):
        via[r['sender']]=dict(kind=r['kind'],money=r['nk'].startswith('money:'),at=r['at'])
    with QLOCK:queued={i['sender']:dict(kind=i['kind'],money=i['cat']=='money') for i in Q if i['kind']!='unsub'}
    rows=con.execute("select * from msg where state='live'").fetchall()
    g={};money=cat=='money';T=types() if money else {}
    want=CATS.get(cat)
    for r in rows:
        labs=r['labels']
        if want and want not in labs:continue
        if cat=='primary' and 'SENT' in labs:continue
        t=None
        if money:
            t=mtype(r,T)
            if not t:continue
            if t=='record':   # count it so the card can say what is kept, but never act on it
                rec=g.setdefault(r['sender'],dict(sender=r['sender'],name=r['name'],n=0,size=0,first=r['date'],last=0,starred=0,unread=0,
                    trashable=0,old=0,old_size=0,subjects=[],unsub='',cats={},types={}))
                rec['types']['record']=rec['types'].get('record',0)+1
                continue
        s=g.setdefault(r['sender'],dict(sender=r['sender'],name='',n=0,size=0,first=r['date'],last=0,starred=0,unread=0,
            trashable=0,old=0,old_size=0,subjects=[],unsub='',cats={},types={}))
        if t:s['types'][t]=s['types'].get(t,0)+1
        s['n']+=1;s['size']+=r['size'];s['first']=min(s['first'],r['date'])
        if r['date']>=s['last']:
            s['last']=r['date'];s['name']=r['name'] or s['name']
            if r['unsub']:s['unsub']=r['unsub']
        if 'STARRED' in labs:s['starred']+=1
        if 'UNREAD' in labs:s['unread']+=1
        if eligible(r):
            s['trashable']+=1
            if (past_keep(r,t) if money else r['date']<time.time()-YEAR):s['old']+=1;s['old_size']+=r['size']
        s['subjects'].append((r['date'],r['subject']))
        for k,v in CATS.items():
            if v in labs:s['cats'][k]=s['cats'].get(k,0)+1
    out=[]
    for s in g.values():
        if s['sender']==me():continue
        raw=s['subjects'];s['subjects']=[t for _,t in sorted(raw,reverse=True)[:6]]
        s['note']=notes.get(notekey(s['sender'],cat))
        s['unsubbed']=uns.get(s['sender'])
        u=s['unsubbed']
        if u and u['status']=='done':   # unsubscribed, yet mail kept coming 3+ days later
            s['still_sending']=sum(1 for d,_ in raw if d>u['at']+3*DAY)
        t=gone.get(s['sender'])
        s['in_trash']=dict(n=t[0],size=t[1] or 0,**via.get(s['sender'],{})) if t else None
        s['queued']=queued.get(s['sender'])
        a=ai.get(s['sender'])
        s['ai']=dict(action=a['action'],category=a['category'],sub=a['subcategory'],conf=a['confidence'],reason=a['reason']) if a else None
        hit=PROTECT.search(' '.join([s['sender'],s['name']]+s['subjects']))
        s['protect']=hit.group(0) if hit else ''
        out.append(s)
    out.sort(key=lambda s:-s['size'])
    return out

def batch_call(ids,method):
    """messages.trash / untrash in HTTP batches of 50, retrying rate-limited ones. Returns ids that succeeded."""
    if os.environ.get('MAILSWIPE_DRYRUN'):
        time.sleep(float(os.environ.get('MAILSWIPE_DRYRUN_DELAY',0)));return list(ids),[]
    ok=[];todo=list(ids);tries=0
    while todo and tries<6:
        failed=[]
        for i in range(0,len(todo),50):
            chunk=todo[i:i+50]
            def cb(rid,resp,exc,_f=failed,_o=ok):
                (_f if exc is not None else _o).append(rid)
            b=svc().new_batch_http_request(callback=cb)
            for m in chunk:b.add(getattr(svc().users().messages(),method)(userId='me',id=m),request_id=m)
            b.execute();time.sleep(0.4)
        todo=failed;tries+=1
        if todo:time.sleep(2*tries)
    return ok,todo

def act(sender,kind,batch=None,cat='all',ids=None):
    try:return _act(sender,kind,batch,cat,ids)
    finally:bump()   # after the change, so a read mid-action can't cache stale data
def _act(sender,kind,batch=None,cat='all',only=None):
    with lock:
        con=db()
        nk=notekey(sender,cat)
        if kind in('keep','skip'):
            con.execute('insert or replace into sender_note values(?,?,?)',(nk,kind,int(time.time())))
            aid=con.execute('insert into action(at,sender,kind,ids,batch,notekey) values(?,?,?,?,?,?)',(int(time.time()),sender,kind,'[]',batch,nk)).lastrowid;con.commit()
            return dict(ok=True,n=0,id=aid)
        if kind=='unnote':
            con.execute('delete from sender_note where sender=?',(sender,));con.commit();return dict(ok=True,n=0)
        rows=[r for r in con.execute("select * from msg where sender=? and state='live'",(sender,)) if eligible(r)]
        if only is not None:   # auto-clean: exactly the emails the plan chose (re-checked as still eligible)
            keepset=set(only);rows=[r for r in rows if r['id'] in keepset and not r['has_att']]
        elif CATS.get(cat):rows=[r for r in rows if CATS[cat] in r['labels'].split(',')]   # only the Gmail tab the card showed
        if only is None and cat=='money':   # alerts/OTP/offers/notices only; records are never touched
            T=types();rows=[(r,mtype(r,T)) for r in rows];rows=[(r,t) for r,t in rows if t in DELETABLE]
            rows=[r for r,t in rows if kind!='trash_old' or past_keep(r,t)]
        elif only is None and kind=='trash_old':rows=[r for r in rows if r['date']<time.time()-YEAR]
        # spending ledger first: alerts/receipts not yet captured are held back (picked up after the next ledger run)
        if con.execute("select count(*) from sqlite_master where name='ledger'").fetchone()[0]:
            T=types();led={x[0] for x in con.execute('select id from ledger where sender=?',(sender,))}
            held=[r for r in rows if mtype(r,T) in('txn_alert','record') and r['id'] not in led]
            if held:rows=[r for r in rows if r not in held]
        else:held=[]
        ids=[r['id'] for r in rows]
        if not ids:return dict(ok=True,n=0,size=0,id=None,held=len(held))
        ok,bad=batch_call(ids,'trash')
        con.executemany("update msg set state='trashed' where id=?",[(i,) for i in ok])
        size=sum(r['size'] for r in rows if r['id'] in set(ok))
        aid=con.execute('insert into action(at,sender,kind,ids,batch,notekey) values(?,?,?,?,?,?)',(int(time.time()),sender,kind,json.dumps(ok),batch,nk)).lastrowid
        con.execute('insert or replace into sender_note values(?,?,?)',(nk,'trashed' if kind=='trash_all' else 'trimmed',int(time.time())))
        con.commit()
        return dict(ok=not bad,n=len(ok),failed=len(bad),size=size,id=aid,held=len(held))

def undo(action_id):
    try:return _undo(action_id)
    finally:bump()
def _undo(action_id):
    with lock:
        con=db();a=con.execute('select * from action where id=?',(action_id,)).fetchone()
        if not a:return dict(ok=False)
        ids=json.loads(a['ids'])
        ok,bad=batch_call(ids,'untrash') if ids else ([],[])
        # Gmail's untrash drops the INBOX label; put it back on the ones that had it.
        inbox=[i for i in ok if 'INBOX' in (con.execute('select labels from msg where id=?',(i,)).fetchone() or [''])[0].split(',')]
        if inbox and not os.environ.get('MAILSWIPE_DRYRUN'):
            for k in range(0,len(inbox),1000):
                svc().users().messages().batchModify(userId='me',body={'ids':inbox[k:k+1000],'addLabelIds':['INBOX']}).execute()
        con.executemany("update msg set state='live' where id=?",[(i,) for i in ok])
        con.execute('delete from sender_note where sender=?',(a['notekey'] or a['sender'],))
        if not bad:con.execute('delete from action where id=?',(action_id,))
        con.commit()
        return dict(ok=not bad,n=len(ok),failed=len(bad))

# ---- unsubscribe: RFC 8058 one-click POST, else mailto via Gmail, else hand the page to the user ----
import urllib.request,urllib.parse,urllib.error,base64
def log_unsub(sender,res,hdr,target):
    c=db();c.execute('insert into unsub_log(sender,at,method,ok,detail,response,target,list_unsub,list_unsub_post,msg) values(?,?,?,?,?,?,?,?,?,?)',
        (sender,int(time.time()),res.get('method'),1 if res.get('ok') else 0,res.get('detail',''),res.get('response','')[:300],target or '',
         hdr['lu'][:1000],hdr['lup'],hdr['msg']));c.commit()
from email.message import EmailMessage
def unsubscribe(sender):
    try:return _unsubscribe(sender)
    except Exception as e:
        log_unsub(sender,dict(method='error',ok=False,detail=f'{type(e).__name__}: {e}'[:300]),dict(lu='',lup='',msg=''),'')
        c=db();c.execute('insert or replace into unsubs values(?,?,?,?,?)',(sender,'error','failed',str(e)[:150],int(time.time())));c.commit();bump()
        return dict(ok=False,method='error',detail=str(e)[:150])
def _unsubscribe(sender):
    con=db()
    ai=con.execute('select category from sender_ai where sender=?',(sender,)).fetchone()
    if ai and ai['category']=='Spam & scams':
        return dict(ok=False,method='refused',detail="Looks like spam: unsubscribing would only confirm your address. Just trash it.")
    row=con.execute("select id from msg where sender=? and unsub!='' order by date desc limit 1",(sender,)).fetchone()
    if not row:return dict(ok=False,method='none',detail='This sender gives no unsubscribe option.')
    h={x['name'].lower():x['value'] for x in svc().users().messages().get(userId='me',id=row['id'],format='metadata',
        metadataHeaders=['List-Unsubscribe','List-Unsubscribe-Post']).execute()['payload'].get('headers',[])}
    hdr=dict(lu=h.get('list-unsubscribe',''),lup=h.get('list-unsubscribe-post',''),msg=row['id'])
    urls=re.findall(r'<([^>]+)>',h.get('list-unsubscribe',''))
    https=next((u for u in urls if u.lower().startswith('https://')),None)
    mailto=next((u for u in urls if u.lower().startswith('mailto:')),None)
    dry=bool(os.environ.get('MAILSWIPE_DRYRUN'))
    res=None
    if https and 'one-click' in h.get('list-unsubscribe-post','').lower():
        try:
            if not dry:
                req=urllib.request.Request(https,data=b'List-Unsubscribe=One-Click',method='POST',
                    headers={'Content-Type':'application/x-www-form-urlencoded','User-Agent':'Mozilla/5.0 MailSwipe'})
                with urllib.request.urlopen(req,timeout=20) as r:code=r.status;body=r.read(300).decode('utf-8','replace')
            else:code=200;body=''
            res=dict(ok=200<=code<400,method='one-click',detail=f'HTTP {code}',response=body)
        except urllib.error.HTTPError as e:
            res=dict(ok=False,method='one-click',detail=f'HTTP {e.code}',response=e.read(300).decode('utf-8','replace'))
        except Exception as e:res=dict(ok=False,method='one-click',detail=str(e)[:150])
        log_unsub(sender,res,hdr,https)
    if (not res or not res['ok']) and mailto:
        u=urllib.parse.urlparse(mailto);q=urllib.parse.parse_qs(u.query)
        m=EmailMessage();m['To']=urllib.parse.unquote(u.path);m['Subject']=q.get('subject',['unsubscribe'])[0]
        m.set_content(q.get('body',['unsubscribe'])[0])
        try:
            if not dry:svc().users().messages().send(userId='me',body={'raw':base64.urlsafe_b64encode(m.as_bytes()).decode()}).execute()
            res=dict(ok=True,method='email',detail='sent to '+m['To'])
        except Exception as e:res=dict(ok=False,method='email',detail=str(e)[:150])
        log_unsub(sender,res,hdr,mailto)
    if not res or not res['ok']:
        page=https or next((u for u in urls if u.lower().startswith('http')),None)
        if page:res=dict(ok=False,method='page',detail='Needs a click on their page',url=page)
        elif not res:res=dict(ok=False,method='none',detail='No usable unsubscribe option.')
        log_unsub(sender,res,hdr,page)
    con.execute('insert or replace into unsubs values(?,?,?,?,?)',(sender,res['method'],'done' if res['ok'] else 'failed',res['detail'],int(time.time())))
    con.commit();bump()
    return res

# ---- background queue: every action goes through one FIFO worker, so the UI never waits on Gmail ----
import collections
Q=collections.deque();QLOCK=threading.Lock();QEV=threading.Event()
QSTAT=dict(current=None,done=0,emails=0,size=0,failed=0,last_error=None)
NOTE={'keep':'keep','skip':'skip','trash_all':'trashed','trash_old':'trimmed'}
_qid=[0]
def enqueue(sender,kind,cat='all',batch=None,ids=None):
    with QLOCK:
        _qid[0]+=1;it=dict(id=_qid[0],sender=sender,kind=kind,cat=cat,batch=batch,ids=ids)
        Q.append(it)
    if kind in NOTE:
        c=db();c.execute('insert or replace into sender_note values(?,?,?)',(notekey(sender,cat),NOTE[kind],int(time.time())));c.commit()  # hide the card now
    bump();QEV.set();return it
def worker():
    while True:
        QEV.wait()
        with QLOCK:
            it=Q[0] if Q else None
            if it:QSTAT['current']=it
            else:QEV.clear();QSTAT['current']=None;continue
        try:
            if it['kind']=='unsub':
                u=unsubscribe(it['sender'])
                QSTAT['unsubbed']=QSTAT.get('unsubbed',0)+(1 if u.get('ok') else 0);continue
            r=act(it['sender'],it['kind'],it['batch'],it['cat'],it.get('ids'))
            QSTAT['emails']+=r.get('n',0);QSTAT['size']+=r.get('size',0) or 0
            if r.get('failed'):QSTAT['failed']+=r['failed'];QSTAT['last_error']=f"{r['failed']} emails from {it['sender']} failed"
            if it['kind'].startswith('trash') and not r.get('n') and r.get('failed'):
                c=db();c.execute('delete from sender_note where sender=?',(notekey(it['sender'],it['cat']),));c.commit()   # nothing moved: bring card back
        except Exception as e:
            QSTAT['failed']+=1;QSTAT['last_error']=f"{it['sender']}: {str(e)[:150]}"
            c=db();c.execute('delete from sender_note where sender=?',(notekey(it['sender'],it['cat']),));c.commit()
        finally:
            with QLOCK:
                if Q and Q[0] is it:Q.popleft()
                QSTAT['done']+=1;QSTAT['current']=None
            bump()
def recover():
    # queue is in memory: senders hidden for a trash that never ran (restart mid-queue) come back for review
    c=db()
    n=c.execute("""delete from sender_note where decision in('trashed','trimmed')
                   and sender not in (select coalesce(notekey,sender) from action where kind like 'trash%')""").rowcount
    c.commit()
    if n:print(f'recovered {n} senders whose queued trash never ran',flush=True)
recover()
threading.Thread(target=worker,daemon=True).start()
def queue_status():
    with QLOCK:
        pend=list(Q)
    trash=[i for i in pend if i['kind'].startswith('trash')]
    cur=QSTAT['current']
    return dict(pending=len(pend),trash_pending=len(trash),unsub_pending=sum(1 for i in pend if i['kind']=='unsub'),unsubbed=QSTAT.get('unsubbed',0),current=cur['sender'] if cur else None,
                emails=QSTAT['emails'],size=QSTAT['size'],failed=QSTAT['failed'],last_error=QSTAT['last_error'])

# ---- AI auto-clean: per-email decision, important stuff kept, the rest trashed ----
PROTECT_CATS=('People','Tax & government','Health','Insurance','Jobs & career','Education')
def auto_plan(detail=False,jury_gate=True):
    """Decide every live email. Returns counts per reason, plus ids grouped by sender when detail=True."""
    con=db();T=types()
    ai={r['sender']:r for r in con.execute('select * from sender_ai')}
    notes={r['sender']:r['decision'] for r in con.execute('select * from sender_note')}
    keep=collections.Counter();trash=collections.Counter();ksz=collections.Counter();tsz=collections.Counter()
    plan={};now=time.time();mine=me()
    jury={(r[0],r[1]):json.loads(r[2]) for r in con.execute('select sender,tmpl,votes from pattern_votes')} if con.execute("select count(*) from sqlite_master where name='pattern_votes'").fetchone()[0] else {}
    use_jury=bool(jury) and jury_gate
    has_ledger=con.execute("select count(*) from sqlite_master where name='ledger'").fetchone()[0]
    ledgered={r[0] for r in con.execute('select id from ledger')} if has_ledger else set()
    for r in con.execute("select id,sender,subject,date,size,labels,has_att from msg where state='live'"):
        labs=r['labels'].split(',');a=ai.get(r['sender']);t=mtype(r,T)
        def K(why):keep[why]+=1;ksz[why]+=r['size']
        if 'STARRED' in labs:K('starred');continue
        if 'SENT' in labs or r['sender']==mine:K('sent by you');continue
        if r['has_att']:K('has attachment');continue
        if notes.get(r['sender'])=='keep' or notes.get('money:'+r['sender'])=='keep':K('you chose Keep');continue
        if not a:K('not classified');continue
        TL={'txn_alert':'old status alerts','otp_security':'old OTP / login alerts','marketing':'promotions','notice':'old notices'}
        cat=a['category']
        if t=='record':K('order / receipt / statement');continue
        if t=='txn_alert' and r['id'] not in ledgered:K('transaction not in spending ledger yet');continue
        if cat in PROTECT_CATS:   # people, tax, health, insurance, jobs, education: only plain promotions may go
            if t=='marketing':why=f'{cat}: promotions'
            else:K(cat.lower());continue
        elif t:
            if a['action'] in('review',) or a['confidence']<0.85:   # sender unclear: only promotions
                if t=='marketing':why=f'{cat}: promotions'
                else:K('AI unsure about sender');continue
            elif past_keep(r,t):why=f'{cat}: {TL[t]}'
            else:K('still within keep time');continue
        elif a['confidence']<0.85:K('AI unsure');continue
        elif a['action']=='trash_all':why=f'{cat}: (no pattern type)'
        elif a['action']=='trash_old' and r['date']<now-YEAR:why=f'{cat}: older than 1 year'
        else:K('AI says keep' if a['action']=='keep' else 'recent / review');continue
        if use_jury:
            v=jury.get((r['sender'],tmpl(r['subject'])))
            if v is None:K('jury: not voted yet');continue
            if v.count('delete')<len(v):K(f"jury: {v.count('keep')} of {len(v)} said keep");continue
        trash[why]+=1;tsz[why]+=r['size']
        if detail:plan.setdefault(r['sender'],[]).append(r['id'])
    out=dict(trash=sorted(([k,v,tsz[k]] for k,v in trash.items()),key=lambda x:-x[2]),keep=sorted(([k,v,ksz[k]] for k,v in keep.items()),key=lambda x:-x[2]),
             trash_n=sum(trash.values()),trash_size=sum(tsz.values()),keep_n=sum(keep.values()),keep_size=sum(ksz.values()))
    if detail:out['plan']=plan
    return out
def auto_sample(n=40,side='trash',jury_gate=True):
    """Random emails from one side of the plan, for checking."""
    import random
    p=auto_plan(detail=True,jury_gate=jury_gate)
    if side=='trash':ids=[i for v in p['plan'].values() for i in v]
    else:
        tr={i for v in p['plan'].values() for i in v}
        ids=[r[0] for r in db().execute("select id from msg where state='live'") if r[0] not in tr]
    pick=random.sample(ids,min(n,len(ids)))
    c=db();q=f"select m.id,m.sender,m.name,m.subject,m.date,m.size,m.has_att,a.category,a.action from msg m left join sender_ai a on a.sender=m.sender where m.id in ({','.join('?'*len(pick))})"
    T=types();out=[]
    for r in c.execute(q,pick):
        d=dict(r);d['type']=T.get((r['sender'],tmpl(r['subject'])));out.append(d)
    return out
IMPORTANT_RE=re.compile(r'invoice|receipt|order (?:confirm|placed|#)|refund|booking|ticket|itinerary|e-?ticket|statement|\btax\b|form ?16|26as|\btds\b|polic(?:y|ies)|premium|salary|payslip|offer letter|interview|warranty|contract note|\bkyc\b|aadhaar|\bpan\b|passport|visa|lease|rent agreement|admission|result|certificate|account (?:closed|locked)',re.I)
def auto_precheck():
    """Final check before a run: deletions that look important, are very recent, or come from protected kinds of senders."""
    p=auto_plan(detail=True);ids=[i for v in p['plan'].values() for i in v]
    c=db();T=types();rows=[]
    for i in range(0,len(ids),900):
        ch=ids[i:i+900]
        rows+=c.execute(f"select m.id,m.sender,m.name,m.subject,m.date,m.size,m.has_att,m.labels,a.category,a.action from msg m left join sender_ai a on a.sender=m.sender where m.id in ({','.join('?'*len(ch))})",ch).fetchall()
    now=time.time()
    groups=dict(words=[],recent=[],cats=[])
    for r in rows:
        d=dict(r);d['type']=T.get((r['sender'],tmpl(r['subject'])));d.pop('labels')
        if d['type']=='marketing' or 'CATEGORY_PROMOTIONS' in r['labels'] or 'CATEGORY_SOCIAL' in r['labels']:
            continue   # ads and social pings never need a second look
        if IMPORTANT_RE.search(r['subject'] or ''):groups['words'].append(d)
        elif now-r['date']<3*86400:groups['recent'].append(d)
        elif r['category'] in PROTECT_CATS:groups['cats'].append(d)
    out={}
    for k,v in groups.items():   # one example per (sender, pattern) so the list shows variety
        seen=set();ex=[]
        for d in sorted(v,key=lambda d:-d['date']):
            key=(d['sender'],tmpl(d['subject']))
            if key in seen:continue
            seen.add(key);ex.append(d)
        out[k]=dict(n=len(v),patterns=len(ex),examples=ex[:40])
    top=collections.Counter(r['sender'] for r in rows).most_common(10)
    return dict(trash_n=p['trash_n'],trash_size=p['trash_size'],senders=len(p['plan']),groups=out,top=top)
def auto_run():
    p=auto_plan(detail=True);b=int(time.time()*1000)
    for snd,ids in p['plan'].items():enqueue(snd,'trash_ids','auto',b,ids)
    c=db();c.execute("insert or replace into settings values('auto_last',?)",(json.dumps(dict(at=int(time.time()),n=p['trash_n'],size=p['trash_size'])),));c.commit()
    return dict(ok=True,senders=len(p['plan']),emails=p['trash_n'],size=p['trash_size'],batch=b)
def setting(k,default=None):
    r=db().execute('select val from settings where key=?',(k,)).fetchone();return json.loads(r[0]) if r else default

JOB={'running':False}
def start_job(title,work):
    if JOB.get('running'):return dict(ok=False,error='Another bulk job is still running')
    JOB.clear();JOB.update(running=True,title=title,done=0,total=0,emails=0,size=0,failed=0,error=None)
    def run():
        try:work(JOB)
        except Exception as e:JOB['error']=str(e)[:300]
        finally:JOB['running']=False;bump()
    threading.Thread(target=run,daemon=True).start()
    return dict(ok=True)

def accept_ai(senders_list,cat='all',unsub=False):
    batch=int(time.time()*1000);n=0;u=0
    for snd in senders_list:
        a=db().execute('select action,category from sender_ai where sender=?',(snd,)).fetchone()
        if a and a['action'] in('trash_all','trash_old','keep'):
            enqueue(snd,a['action'],cat,batch);n+=1
            if unsub and a['action']=='trash_all' and a['category']!='Spam & scams':enqueue(snd,'unsub',cat,batch);u+=1
    return dict(ok=True,queued=n,unsub=u,batch=batch)

def undo_last_job():
    with QLOCK:
        pend=[i for i in Q if i is not QSTAT['current']]
        if pend:
            last=pend[-1]
            drop=[i for i in pend if last['batch'] and i['batch']==last['batch']] or [last]
            for i in drop:Q.remove(i)
        else:drop=[]
    if drop:
        b=drop[0]['batch']
        while b and QSTAT['current'] and QSTAT['current'].get('batch')==b:time.sleep(0.2)   # in-flight item of this batch: let it land, then undo it too
        c=db();c.executemany('delete from sender_note where sender=?',[(notekey(i['sender'],i['cat']),) for i in drop]);c.commit();bump()
        done_in_batch=c.execute('select count(*) from action where batch=?',(drop[0]['batch'],)).fetchone()[0] if drop[0]['batch'] else 0
        r=dict(ok=True,cancelled=len(drop),sender=drop[0]['sender'] if len(drop)==1 else f'{len(drop)} senders',kind=drop[0]['kind'])
        if done_in_batch:   # part of the batch already ran: undo those too
            r.update(undo_last_job());r['cancelled']=len(drop)
        return r
    while QSTAT['current']:time.sleep(0.2)   # let the running item finish, then undo it normally
    con=db();a=con.execute('select id,sender,kind,batch from action order by id desc limit 1').fetchone()
    if not a:return dict(ok=False,none=True)
    if not a['batch']:
        r=undo(a['id']);r.update(sender=a['sender'],kind=a['kind']);return r
    ids=[r['id'] for r in con.execute('select id from action where batch=?',(a['batch'],))]
    def work(j):
        j['total']=len(ids)
        for i in ids:
            r=undo(i);j['done']+=1;j['emails']+=r.get('n',0);j['failed']+=r.get('failed',0)
    r=start_job('Undoing bulk action',work);r.update(job=True,senders=len(ids));return r

def history():
    con=db();out=[]
    for a in con.execute("select * from action where kind like 'trash%' order by at desc limit 500"):
        ids=json.loads(a['ids'])
        sz=con.execute(f"select coalesce(sum(size),0),max(name) from msg where id in ({','.join('?'*len(ids))})",ids).fetchone() if ids else (0,'')
        out.append(dict(id=a['id'],at=a['at'],sender=a['sender'],name=sz[1] or '',kind=a['kind'],n=len(ids),size=sz[0]))
    kept=[dict(sender=r['sender'],decision=r['decision'],at=r['at']) for r in con.execute(
        "select * from sender_note where decision in('keep','skip','trimmed') order by at desc")]
    return dict(trashed=out,kept=kept)

def stats():
    con=db()
    live=con.execute("select count(*),coalesce(sum(size),0) from msg where state='live'").fetchone()
    tr=con.execute("select count(*),coalesce(sum(size),0) from msg where state='trashed'").fetchone()
    log=open(LOG).read().split('\n') if os.path.exists(LOG) else []
    indexing=not any(l.startswith('DONE') for l in log)
    with QLOCK:pend=list(Q)
    if pend:
        l=pend[-1];n=sum(1 for i in pend if l['batch'] and i['batch']==l['batch']) or 1
        return dict(live=live[0],live_size=live[1],trashed=tr[0],trashed_size=tr[1],indexing=indexing,total=profile().get('messagesTotal',0),
                    actions=1,last=dict(sender=l['sender'] if n==1 else f'{n} senders',kind=l['kind'] if n==1 else 'bulk',queued=True))
    last=con.execute('select sender,kind,batch from action order by id desc limit 1').fetchone()
    if last and last['batch']:
        n=con.execute('select count(*) from action where batch=?',(last['batch'],)).fetchone()[0]
        last=dict(sender=f'{n} senders',kind='bulk',batch=last['batch'])
    return dict(live=live[0],live_size=live[1],trashed=tr[0],trashed_size=tr[1],indexing=indexing,total=profile().get('messagesTotal',0),
                actions=con.execute('select count(*) from action').fetchone()[0],last=dict(last) if last else None)

class H(BaseHTTPRequestHandler):
    def log_message(s,*a):pass
    def send(s,code,obj,ct='application/json'):
        body=obj if isinstance(obj,bytes) else json.dumps(obj).encode()
        s.send_response(code);s.send_header('Content-Type',ct);s.send_header('Content-Length',str(len(body)))
        s.send_header('Cache-Control','no-store');s.end_headers();s.wfile.write(body)
    def do_GET(s):
        u=urlparse(s.path);q=parse_qs(u.query)
        if u.path=='/':return s.send(200,open(os.path.join(HERE,'index.html'),'rb').read(),'text/html; charset=utf-8')
        if u.path=='/senders':return s.send(200,senders(q.get('cat',['promotions'])[0]))
        if u.path=='/history':return s.send(200,history())
        if u.path=='/stats':return s.send(200,stats())
        if u.path=='/auto/plan':return s.send(200,dict(auto_plan(),daily=setting('auto_daily',False),last=setting('auto_last'),audit=setting('auto_audit')))
        if u.path=='/auto/precheck':return s.send(200,auto_precheck())
        if u.path=='/auto/sample':return s.send(200,auto_sample(int(q.get('n',['40'])[0]),q.get('side',['trash'])[0],q.get('jury',['1'])[0]!='0'))
        if u.path=='/spending':return s.send(200,open(os.path.join(HERE,'spending.html'),'rb').read(),'text/html; charset=utf-8')
        if u.path=='/ledger/rules':
            import ledger as L;return s.send(200,L.rules(db()))
        if u.path=='/ledger':
            rows=[dict(r) for r in db().execute("select id,date,amount,direction,merchant,instrument,source,sender,subject,currency,category from ledger where parsed=1 order by date desc")]
            return s.send(200,rows)
        if u.path=='/auto/overrides':
            c=db();out=[]
            for r in c.execute("select sender,tmpl,at from mail_type where model='user' and (sender,tmpl) not in (select sender,tmpl from pattern_override)").fetchall():
                c.execute('insert into pattern_override values(?,?,?,?,?)',(r['sender'],r['tmpl'],None,None,r['at']))   # rules made before undo existed
            c.commit()
            for o in c.execute('select * from pattern_override order by at desc').fetchall():
                n=sum(1 for r in c.execute("select subject from msg where sender=? and state='live'",(o['sender'],)) if tmpl(r['subject'])==o['tmpl'])
                out.append(dict(sender=o['sender'],tmpl=o['tmpl'],prev=o['prev_type'],at=o['at'],emails=n))
            return s.send(200,out)
        if u.path=='/unsub_log':
            rows=[dict(r) for r in db().execute('select * from unsub_log order by id desc limit 300')]
            return s.send(200,rows)
        if u.path=='/queue':return s.send(200,queue_status())
        if u.path=='/job':return s.send(200,{k:v for k,v in JOB.items()})
        s.send(404,{})
    def do_POST(s):
        d=json.loads(s.rfile.read(int(s.headers.get('Content-Length',0))) or b'{}')
        try:
            if s.path=='/act':
                if d['kind']=='unnote':return s.send(200,act(d['sender'],'unnote'))
                it=enqueue(d['sender'],d['kind'],d.get('cat','all'));return s.send(200,dict(ok=True,queued=True,qid=it['id']))
            if s.path=='/undo':return s.send(200,undo(int(d['id'])))
            if s.path=='/undo_last':return s.send(200,undo_last_job())
            if s.path=='/accept_ai':return s.send(200,accept_ai(d['senders'],d.get('cat','all'),d.get('unsub',False)))
            if s.path=='/accept_rules':
                b=int(time.time()*1000)
                for snd in d['senders']:enqueue(snd,'trash_old','money',b)
                return s.send(200,dict(ok=True,queued=len(d['senders']),batch=b))
            if s.path=='/auto/run':return s.send(200,auto_run())
            if s.path=='/auto/keep_pattern':   # user override: this subject pattern is a record, never auto-deleted
                c=db();m=c.execute('select sender,subject from msg where id=?',(d['id'],)).fetchone()
                if not m:return s.send(404,dict(ok=False))
                k=(m['sender'],tmpl(m['subject']))
                prev=c.execute('select type,model from mail_type where sender=? and tmpl=?',k).fetchone()
                if not c.execute('select 1 from pattern_override where sender=? and tmpl=?',k).fetchone():   # remember what it was, for undo
                    c.execute('insert into pattern_override values(?,?,?,?,?)',(k[0],k[1],prev['type'] if prev and prev['model']!='user' else None,prev['model'] if prev and prev['model']!='user' else None,int(time.time())))
                c.execute("insert or replace into mail_type values(?,?,'record','user',?)",(k[0],k[1],int(time.time())));c.commit()
                n=sum(1 for r in c.execute("select subject from msg where sender=? and state='live'",(m['sender'],)) if tmpl(r['subject'])==tmpl(m['subject']))
                return s.send(200,dict(ok=True,pattern=tmpl(m['subject']),sender=m['sender'],emails=n))
            if s.path=='/auto/unkeep_pattern':   # undo a 'Keep this kind'
                c=db();k=(d['sender'],d['tmpl'])
                o=c.execute('select * from pattern_override where sender=? and tmpl=?',k).fetchone()
                if not o:return s.send(200,dict(ok=False,error='no such rule'))
                if o['prev_type']:c.execute('insert or replace into mail_type values(?,?,?,?,?)',(k[0],k[1],o['prev_type'],o['prev_model'],int(time.time())))
                else:c.execute('delete from mail_type where sender=? and tmpl=?',k)   # re-typed by the next classify run
                c.execute('delete from pattern_override where sender=? and tmpl=?',k);c.commit()
                return s.send(200,dict(ok=True,restored=o['prev_type']))
            if s.path=='/auto/daily':
                c=db();c.execute("insert or replace into settings values('auto_daily',?)",(json.dumps(bool(d.get('on'))),));c.commit()
                return s.send(200,dict(ok=True,daily=bool(d.get('on'))))
            if s.path in('/ledger/rule_add','/ledger/rule_del'):
                import ledger as L;c=db();L.rules(c)
                if s.path=='/ledger/rule_add':
                    if not (d.get('match') or '').strip():return s.send(400,dict(ok=False,error='empty match'))
                    c.execute('insert into ledger_rule(match,field,set_direction,set_category,rename,created) values(?,?,?,?,?,?)',
                        (d['match'].strip(),d.get('field') or 'any',d.get('set_direction') or None,d.get('set_category') or None,d.get('rename') or None,int(time.time())))
                else:c.execute('delete from ledger_rule where id=?',(int(d['id']),))
                c.commit();n,ok=L.reparse()   # re-derive the whole ledger with the new rule set (a few seconds)
                hit=c.execute("select count(*) from ledger where parsed=1 and (lower(merchant||' '||subject||' '||snippet||' '||sender) like ?)",('%'+(d.get('match') or '').lower()+'%',)).fetchone()[0] if s.path=='/ledger/rule_add' else 0
                return s.send(200,dict(ok=True,matched=hit))
            if s.path=='/unsubscribe':return s.send(200,unsubscribe(d['sender']))
        except Exception as e:
            return s.send(500,dict(ok=False,error=str(e)[:300]))
        s.send(404,{})
ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()

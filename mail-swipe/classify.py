#!/usr/bin/env python3
"""AI sender classifier for Mail Swipe.

For each sender: category, subcategory, a recommended action (trash_all / trash_old / keep / review),
confidence and a one-line reason. Results go into index.db:sender_ai, which the UI shows on each card.

Only METADATA leaves the machine: sender name + address, counts, unread %, Gmail tabs, date range and up to
8 recent subject lines. Never message bodies.

  classify.py               classify senders that are new or whose mail count changed by 20%+
  classify.py --all         re-classify everyone
  classify.py --limit 40    try a few first
Env: LLM_BASE_URL (default codex-lb http://127.0.0.1:2455/v1), LLM_MODEL (gpt-6-luna), LLM_EFFORT (low),
     LLM_API_KEY or OPENAI_API_KEY (required for classification).
"""
import json,os,re,sys,time,urllib.request,urllib.error,concurrent.futures as cf
from gmailapi import db

BASE=os.environ.get('LLM_BASE_URL','http://127.0.0.1:2455/v1').rstrip('/')
MODEL=os.environ.get('LLM_MODEL','gpt-6-luna')
EFFORT=os.environ.get('LLM_EFFORT','low')
BATCH=20
CATEGORIES=['Shopping','Food & delivery','Travel & transport','Banking & cards','Investing & trading','Tax & government',
 'Insurance','Bills & utilities','Health','Jobs & career','Education','Social media','News & newsletters',
 'Tech & developer','Security & accounts','Entertainment','People','Spam & scams','Other']
ACTIONS=['trash_all','trash_old','keep','review']

PROMPT=f"""You triage a personal Gmail inbox (India-based user) to free storage. You get senders with metadata.
For EACH sender return: category (one of {CATEGORIES}), subcategory (2-4 words, e.g. "marketing offers",
"order & delivery updates", "OTP & login alerts", "account statements", "job alerts"), action, confidence (0-1),
reason (max 12 words, plain English).

Actions:
- trash_all: pure marketing/promotions, newsletters never read, social notifications, spam, expired one-off services.
- trash_old: transactional mail from a service still in use (orders, rides, delivery, app alerts): recent is useful, >1 year old is clutter.
- keep: real people, bank/card/demat/tax/insurance statements and contract notes, government/ID, medical, legal,
  job offers/payslips, receipts likely needed for tax or warranty, anything irreplaceable.
- review: genuinely unsure, or mixed important + junk.
Be conservative with money, tax, identity and health: prefer keep or trash_old over trash_all there.
"Never opened" percentage is a strong signal for marketing.
Reply ONLY with JSON: {{"results":[{{"id":<id>,"category":...,"subcategory":...,"action":...,"confidence":...,"reason":...}}]}}"""

def api_key():
    k=os.environ.get('LLM_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if k:return k
    raise SystemExit('no API key')
KEY=api_key()

def chat(content):
    body=dict(model=MODEL,reasoning_effort=EFFORT,messages=[{'role':'system','content':PROMPT},{'role':'user','content':content+'\n\nReturn the json object now.'}],
              response_format={'type':'json_object'})
    req=urllib.request.Request(BASE+'/chat/completions',data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=180) as r:d=json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read()[:300]!r}') from None
    txt=d['choices'][0]['message']['content']
    txt=re.sub(r'^```(json)?|```$','',txt.strip()).strip()
    return json.loads(txt)['results'],d.get('usage',{})

def senders():
    con=db();out={}
    for r in con.execute("select sender,name,subject,date,size,labels from msg where state='live' order by date desc"):
        s=out.setdefault(r['sender'],dict(sender=r['sender'],name=r['name'],n=0,size=0,unread=0,first=r['date'],last=r['date'],tabs={},subjects=[]))
        s['n']+=1;s['size']+=r['size'];s['first']=min(s['first'],r['date'])
        if 'UNREAD' in r['labels']:s['unread']+=1
        for t in ('PROMOTIONS','SOCIAL','UPDATES','FORUMS','PERSONAL'):
            if 'CATEGORY_'+t in r['labels']:s['tabs'][t.lower()]=s['tabs'].get(t.lower(),0)+1
        if len(s['subjects'])<8 and r['subject'] not in s['subjects']:s['subjects'].append(r['subject'][:110])
    return out

def describe(i,s):
    y=lambda t:time.strftime('%Y-%m',time.localtime(t))
    return (f"[{i}] {s['name'] or '-'} <{s['sender']}> | {s['n']} emails, {s['size']/1e6:.1f} MB, "
            f"{round(100*s['unread']/s['n'])}% never opened, {y(s['first'])}..{y(s['last'])}, tabs {s['tabs']}\n"
            + '\n'.join('   - '+t for t in s['subjects']))

def main():
    args=sys.argv[1:];limit=int(args[args.index('--limit')+1]) if '--limit' in args else None
    con=db()
    con.execute('''create table if not exists sender_ai(sender text primary key,category text,subcategory text,action text,
        confidence real,reason text,n_at integer,model text,at integer)''');con.commit()
    done={r['sender']:r['n_at'] for r in con.execute('select sender,n_at from sender_ai')}
    S=senders()
    todo=[s for s in S.values() if '--all' in args or s['sender'] not in done or abs(s['n']-done[s['sender']])>max(2,0.2*done[s['sender']])]
    todo.sort(key=lambda s:-s['size'])
    if limit:todo=todo[:limit]
    print(f'{len(S)} senders, {len(todo)} to classify with {MODEL} ({EFFORT})',flush=True)
    batches=[todo[i:i+BATCH] for i in range(0,len(todo),BATCH)]
    tokens=[0];ok=[0];bad=[0]
    def run(batch):
        content='\n\n'.join(describe(i,s) for i,s in enumerate(batch))
        for attempt in range(3):
            try:
                res,usage=chat(content);break
            except Exception as e:
                err=e;time.sleep(5*(attempt+1))
        else:
            print('batch failed:',str(err)[:200],flush=True);bad[0]+=len(batch);return
        tokens[0]+=usage.get('total_tokens',0)
        rows=[]
        for r in res:
            try:
                s=batch[int(r['id'])]
                act=r['action'] if r['action'] in ACTIONS else 'review'
                cat=r['category'] if r['category'] in CATEGORIES else 'Other'
                rows.append((s['sender'],cat,str(r.get('subcategory',''))[:40],act,float(r.get('confidence',0.5)),
                             str(r.get('reason',''))[:120],s['n'],MODEL,int(time.time())))
            except Exception:continue
        c=db();c.executemany('insert or replace into sender_ai values(?,?,?,?,?,?,?,?,?)',rows);c.commit()
        ok[0]+=len(rows);bad[0]+=len(batch)-len(rows)
        print(f'{ok[0]}/{len(todo)} classified, {bad[0]} missed, {tokens[0]:,} tokens',flush=True)
    with cf.ThreadPoolExecutor(4) as ex:list(ex.map(run,batches))
    print('DONE',ok[0],'classified,',bad[0],'missed')

if __name__=='__main__':main()

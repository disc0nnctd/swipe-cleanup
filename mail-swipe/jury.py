#!/usr/bin/env python3
"""Jury for AI auto-clean: every subject pattern the plan would delete gets N independent votes
(different reviewer roles, shuffled batches). Auto-clean only deletes a pattern when ALL votes say delete;
a split vote keeps it. Votes are stored in index.db:pattern_votes and reused (only new patterns are voted on).

  jury.py            vote on planned-deletion patterns that have no votes yet
  jury.py --redo     vote on everything again
"""
import json,random,sys,time,urllib.request,concurrent.futures as cf
from gmailapi import db,tmpl
import classify

B='http://127.0.0.1:8092'
ROLES=[
 "You are a cautious archivist. Keep anything the user might ever need to look up again.",
 "You are a storage-minded inbox cleaner. Delete clutter, but never delete records of money, purchases, bookings, accounts or people.",
 "You imagine the user searching their mail in two years. Keep it only if a real person would plausibly search for this email.",
]
POLICY="""User's rules: KEEP orders, invoices, receipts, payment confirmations, refunds, bookings/tickets, statements,
contract notes, tax/government/ID, health, insurance, job/education matters, account-security events that may still matter,
and anything from a real person. DELETE promotions, newsletters, social notifications, old OTPs, old routine status
alerts (card/UPI alerts, delivery status) and expired notices.
Each line: [id] sender | example subject | date of latest email | how many emails share the pattern.
Reply ONLY with json: {"votes":[{"id":<id>,"v":"delete" or "keep"}]} with one vote for every id."""

def vote(role_i,batch,info):
    lines='\n'.join(f"[{i}] {k[0]} | {info[k]['subject'][:120]} | {info[k]['date']} | {info[k]['n']}" for i,k in enumerate(batch))
    body=dict(model=classify.MODEL,reasoning_effort=classify.EFFORT,response_format={'type':'json_object'},
        messages=[{'role':'system','content':ROLES[role_i]+'\n'+POLICY},{'role':'user','content':lines+'\n\nReturn the json object now.'}])
    for attempt in range(3):
        try:
            req=urllib.request.Request(classify.BASE+'/chat/completions',data=json.dumps(body).encode(),
                headers={'Authorization':'Bearer '+classify.KEY,'Content-Type':'application/json'})
            d=json.load(urllib.request.urlopen(req,timeout=240))
            out={}
            for v in json.loads(d['choices'][0]['message']['content'])['votes']:
                try:out[batch[int(v['id'])]]=v['v'] if v['v'] in('delete','keep') else 'keep'
                except Exception:pass
            return out,d.get('usage',{}).get('total_tokens',0)
        except Exception as e:err=e;time.sleep(5*(attempt+1))
    print('batch failed',str(err)[:120],flush=True);return {},0

def main():
    con=db()
    con.execute('create table if not exists pattern_votes(sender text,tmpl text,votes text,at integer,primary key(sender,tmpl))');con.commit()
    have={(r[0],r[1]) for r in con.execute('select sender,tmpl from pattern_votes')}
    sample=json.load(urllib.request.urlopen(B+'/auto/sample?n=1000000&side=trash&jury=0',timeout=900))   # plan before the jury gate
    info={}
    for m in sample:
        k=(m['sender'],tmpl(m['subject']));i=info.setdefault(k,dict(subject=m['subject'],date=m['date'],n=0))
        i['n']+=1
        if m['date']>i['date']:i.update(subject=m['subject'],date=m['date'])
    for i in info.values():i['date']=time.strftime('%Y-%m-%d',time.localtime(i['date']))
    todo=[k for k in info if '--redo' in sys.argv or k not in have]
    print(f'{len(info)} patterns planned for deletion, {len(todo)} to vote on x{len(ROLES)}',flush=True)
    votes={k:[None]*len(ROLES) for k in todo};tok=[0];done=[0]
    jobs=[]
    for r in range(len(ROLES)):
        order=todo[:];random.Random(r).shuffle(order)   # each juror sees different neighbours
        jobs+=[(r,order[i:i+100]) for i in range(0,len(order),100)]
    def run(job):
        r,batch=job;out,t=vote(r,batch,info);tok[0]+=t
        for k,v in out.items():votes[k][r]=v
        done[0]+=1
        if done[0]%20==0:print(f'{done[0]}/{len(jobs)} batches, {tok[0]:,} tokens',flush=True)
    with cf.ThreadPoolExecutor(8) as ex:list(ex.map(run,jobs))
    rows=[(k[0],k[1],json.dumps(v),int(time.time())) for k,v in votes.items() if all(x is not None for x in v)]
    c=db();c.executemany('insert or replace into pattern_votes values(?,?,?,?)',rows);c.commit()
    split=sum(1 for _,_,v,_ in rows if json.loads(v).count('delete')<len(ROLES))
    print(f'DONE {len(rows)} patterns voted ({len(todo)-len(rows)} incomplete, retried next run); {split} split -> kept')

if __name__=='__main__':main()

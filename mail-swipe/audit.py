#!/usr/bin/env python3
"""Second-opinion audit of the auto-clean plan: a random sample of emails it would trash goes to the model
with a deliberately paranoid prompt. Anything flagged is shown in the UI before you run auto-clean.
  audit.py [N]   (default 300)
"""
import json,sys,time,urllib.request
import classify
B='http://127.0.0.1:8092'
N=int(sys.argv[1]) if len(sys.argv)>1 else 300
sample=json.load(urllib.request.urlopen(f'{B}/auto/sample?n={N}&side=trash',timeout=300))
PROMPT="""You are auditing an automatic email cleanup for an India-based user. Each line is an email it plans to DELETE:
sender, display name, subject, date, sender category. Be paranoid: flag anything that might be worth keeping:
receipts/invoices/warranty, bookings or tickets for future dates, account or legal notices, money owed or refunds,
government/tax/ID, health, a real person writing, job or education matters, passwords/account recovery.
Do NOT flag ordinary marketing, newsletters, social notifications, deals, old OTPs or routine transaction alerts.
Reply ONLY with json: {"flagged":[{"id":<id>,"why":"<max 10 words>"}]}  (empty list if nothing)."""
flags=[]
for i in range(0,len(sample),60):
    chunk=sample[i:i+60]
    lines='\n'.join(f"[{j}] {m['sender']} | {m['name'] or '-'} | {m['subject'][:120]} | {time.strftime('%Y-%m-%d',time.localtime(m['date']))} | {m['category']}" for j,m in enumerate(chunk))
    body=dict(model=classify.MODEL,reasoning_effort=classify.EFFORT,response_format={'type':'json_object'},
              messages=[{'role':'system','content':PROMPT},{'role':'user','content':lines+'\n\nReturn the json object now.'}])
    req=urllib.request.Request(classify.BASE+'/chat/completions',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+classify.KEY,'Content-Type':'application/json'})
    res=json.loads(json.load(urllib.request.urlopen(req,timeout=180))['choices'][0]['message']['content'])['flagged']
    for f in res:
        try:m=chunk[int(f['id'])];flags.append(dict(id=m['id'],sender=m['sender'],subject=m['subject'],category=m['category'],why=f['why'],date=m['date']))
        except Exception:pass
out=dict(at=int(time.time()),sampled=len(sample),flagged=flags)
from gmailapi import db
c=db();c.execute("insert or replace into settings values('auto_audit',?)",(json.dumps(out),));c.commit()
print(f'{len(flags)} of {len(sample)} sampled deletions flagged ({100*len(flags)/max(1,len(sample)):.1f}%)')
for f in flags:print(f"  {f['category'][:18]:18} {f['sender'][:30]:30} | {f['subject'][:60]} -> {f['why']}")

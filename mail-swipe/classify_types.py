#!/usr/bin/env python3
"""Classify finance-sender subject PATTERNS into mail types, so transaction alerts / OTPs / offers can be
deleted while statements and records are always kept.

Types: txn_alert, otp_security, marketing, notice, record   (retention in gmailapi.TYPE_KEEP)
Only the sender address and one example subject per pattern are sent to the model.
  classify_types.py            new patterns only
  classify_types.py --all      everything again
"""
import collections,json,sys,time,concurrent.futures as cf
from gmailapi import db,tmpl,MONEY_CATS
import classify   # reuses chat(), model settings and the API key

TYPES=['txn_alert','otp_security','marketing','notice','record']
PROMPT="""You label email subject patterns for an India-based user's inbox cleanup (banks, brokers, payment apps, shops,
food delivery, travel, games, apps, services). Each line: [id] sender | example subject | how many emails share this pattern.
Types:
- txn_alert: routine per-event status pings with no lasting value: card/UPI debit or credit alerts, trade executed,
  demat debit/credit, "money added", order shipped / out for delivery / delivered, ride or food-delivery status,
  "rate your order", standing-instruction run, cashback credited.
- otp_security: OTPs, verification codes, new login/device alerts, password/PIN changes, card blocked/unblocked.
- marketing: offers, loans, card upgrades, limit-increase offers, product promotions, newsletters, market commentary, webinars.
- notice: general service notices: KYC reminders, terms/rate changes, e-voting/AGM notices, maintenance, "update your details".
- record: anything worth keeping: ORDER CONFIRMATIONS, INVOICES, RECEIPTS, "you paid", payment confirmations,
  REFUNDS and CANCELLATIONS (money involved), bookings/tickets/itineraries, subscription or purchase receipts (Steam, app
  stores), warranties, account/card statements, contract notes, holdings statements, tax documents (TDS, Form 16, 26AS,
  capital gains), policy documents, premium receipts, loan letters, complaint/support threads, account locked/closed notices.
When unsure between record and anything else, choose record.
Reply ONLY with json: {"results":[{"id":<id>,"type":"<type>"}]}"""

def main():
    con=db()
    if '--everyone' in sys.argv:   # every non-personal sender (for AI auto-clean); attachments and sent mail are protected anyway
        rows=con.execute("select m.sender,m.subject from msg m join sender_ai a on a.sender=m.sender where a.category!='People' and m.has_att=0 and m.labels not like '%SENT%'").fetchall()
    else:
        F=MONEY_CATS
        rows=con.execute(f"select m.sender,m.subject from msg m join sender_ai a on a.sender=m.sender where a.category in ({','.join('?'*len(F))})",F).fetchall()
    pats=collections.Counter();example={}
    for r in rows:
        k=(r['sender'],tmpl(r['subject']));pats[k]+=1;example.setdefault(k,r['subject'][:140])
    done={(r['sender'],r['tmpl']) for r in con.execute('select sender,tmpl from mail_type')}
    user={(r['sender'],r['tmpl']) for r in con.execute("select sender,tmpl from mail_type where model='user'")}   # your overrides always win
    todo=[k for k,_ in pats.most_common() if k not in user and ('--all' in sys.argv or k not in done)]
    print(f'{len(pats)} patterns over {len(rows)} finance emails, {len(todo)} to classify',flush=True)
    batches=[todo[i:i+80] for i in range(0,len(todo),80)];ok=[0];tok=[0]
    def run(batch):
        content='\n'.join(f'[{i}] {k[0]} | {example[k]} | {pats[k]}' for i,k in enumerate(batch))
        for attempt in range(3):
            try:
                body=dict(model=classify.MODEL,reasoning_effort=classify.EFFORT,response_format={'type':'json_object'},
                    messages=[{'role':'system','content':PROMPT},{'role':'user','content':content+'\n\nReturn the json object now.'}])
                import urllib.request
                req=urllib.request.Request(classify.BASE+'/chat/completions',data=json.dumps(body).encode(),
                    headers={'Authorization':'Bearer '+classify.KEY,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=180) as r:d=json.load(r)
                res=json.loads(d['choices'][0]['message']['content'])['results'];tok[0]+=d.get('usage',{}).get('total_tokens',0);break
            except Exception as e:
                err=e;time.sleep(5*(attempt+1))
        else:print('batch failed',str(err)[:150],flush=True);return
        out=[]
        for r in res:
            try:k=batch[int(r['id'])];t=r['type'] if r['type'] in TYPES else 'record';out.append((k[0],k[1],t,classify.MODEL,int(time.time())))
            except Exception:pass
        c=db();c.executemany('insert or replace into mail_type values(?,?,?,?,?)',out);c.commit()
        ok[0]+=len(out);print(f'{ok[0]}/{len(todo)} patterns, {tok[0]:,} tokens',flush=True)
    with cf.ThreadPoolExecutor(6) as ex:list(ex.map(run,batches))
    print('DONE',ok[0])

if __name__=='__main__':main()

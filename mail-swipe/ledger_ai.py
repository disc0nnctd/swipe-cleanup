#!/usr/bin/env python3
"""Categorise spending merchants with the LOCAL model (llama.cpp on this machine; nothing leaves it).
Only merchant names are sent, never amounts. Results go to merchant_category and are used by
ledger.reparse() when the built-in keyword list has no match. Your own rules still win.

  ledger_ai.py            merchants not categorised yet
  ledger_ai.py --all      everything again
Env: LOCAL_LLM_URL (default http://127.0.0.1:8084/v1)
"""
import json,os,re,sys,time,urllib.request
from gmailapi import db
import ledger
URL=os.environ.get('LOCAL_LLM_URL','http://127.0.0.1:8084/v1').rstrip('/')
CATS=[c for c,_ in ledger.CATEGORY]+['Restaurants & cafes','People (UPI)','Education','Fees & charges','Other']
PROMPT=f"""Classify Indian payment merchant names (from bank card/UPI alerts) into exactly one category from:
{', '.join(CATS)}.
Hints: names of individual people or shop owners paid by UPI -> People (UPI); restaurants, cafes, bars, sweet shops,
momos, bakeries -> Restaurants & cafes; RAZ*/PAYU*/CYBS prefixes are payment gateways, judge by the rest of the name.
Reply ONLY with JSON: {{"r":[{{"i":<index>,"c":"<category>"}}]}}"""

def ask(names):
    lines='\n'.join(f'{i}. {n}' for i,n in enumerate(names))
    body=dict(model='local',temperature=0,max_tokens=1600,messages=[{'role':'system','content':PROMPT},{'role':'user','content':lines}])
    req=urllib.request.Request(URL+'/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    txt=json.load(urllib.request.urlopen(req,timeout=900))['choices'][0]['message']['content']
    txt=txt[txt.find('{'):txt.rfind('}')+1]
    return json.loads(txt)['r']

def main():
    c=db()
    c.execute('create table if not exists merchant_category(merchant text primary key,category text,source text,at integer)')
    done={r[0] for r in c.execute('select merchant from merchant_category')}
    names=sorted({(r[0] or '').upper().strip() for r in c.execute("select merchant from ledger where parsed=1 and direction='debit' and merchant!='' and category in ('Other','')")})
    if '--all' not in sys.argv:names=[n for n in names if n not in done]
    print(f'{len(names)} merchants to categorise locally',flush=True)
    for i in range(0,len(names),30):
        chunk=names[i:i+30]
        for attempt in range(2):
            try:res=ask(chunk);break
            except Exception as e:err=e;res=None
        if res is None:print('batch failed',str(err)[:120],flush=True);continue
        rows=[]
        for r in res:
            try:
                cat=r['c'] if r['c'] in CATS else 'Other'
                rows.append((chunk[int(r['i'])],cat,'local-ai',int(time.time())))
            except Exception:pass
        c.executemany('insert or replace into merchant_category values(?,?,?,?)',rows);c.commit()
        print(f'{min(i+30,len(names))}/{len(names)}',flush=True)
    print('DONE')

if __name__=='__main__':main()

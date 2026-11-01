#!/usr/bin/env python3
"""Spending ledger: every transaction alert and receipt becomes a row (date, amount, debit/credit, merchant,
card/account) BEFORE auto-clean is allowed to delete it. Parsing is local regex on Gmail's preview text; no AI,
nothing leaves the machine.

  ledger.py            fetch previews for new alert/receipt emails and parse them
  ledger.py --reparse  re-run the parser on stored previews (after improving the rules)
  ledger.py --csv F    export the ledger to a CSV
"""
import csv,html,re,sys,time
from gmailapi import service,db,tmpl

AMT=re.compile(r'(rs\.?|inr|₹|usd|us\$|\$)\s*(\d[\d,]*(?:\.\d{1,2})?)',re.I)
USD_INR=88.0   # rough rate for totals; the original USD amount is kept in the row
BALANCE=re.compile(r'(bal(?:ance)?|avl|available|limit|outstanding|due)\W{0,12}$',re.I)
INSTR=re.compile(r'(?:card|account|a/c|acct)\b[^0-9]{0,25}?(?:xx|\*+|ending(?: with)?|no\.?)?\s*(\d{4})\b',re.I)
MERCHANT=[re.compile(p,re.I) for p in (
    r'\bat\s+([A-Za-z0-9][\w .&*\'/-]{2,40}?)\s+on\b',
    r'\btowards\s+(?:VPA\s+\S+\s+\()?([A-Za-z0-9][\w .&*\'/@-]{2,40}?)\)?\s+on\b',
    r'\bto\s+VPA\s+\S+\s+([A-Za-z][\w .&\'-]{2,40}?)\s+on\b',
    r'\bInfo:\s*([^.]{3,40})',
    r'\bpaid\s+(?:rs\.?|inr|₹)\s*[\d,.]+\s+to\s+([A-Za-z][\w .&\'-]{2,40}?)(?:\s+on\b|[.,]|$)',
    r'\bfrom\s+([A-Za-z][\w .&\'-]{2,40}?)\s+(?:on|has|via)\b',
    r"\bto\s+([A-Z][A-Za-z .]{2,40}?)'s\b",
    r'receipt for\s+([A-Za-z][\w .&\'-]{2,40})',
    r'^([A-Z][A-Za-z0-9 .&\'()-]{2,50}?)\s+(?:₹|rs\.?)\s*[\d,.]+\s+paid successfully',
    r'\bpayment to\s+([A-Za-z][\w .&\'-]{2,40}?)\s+was\b',
    r'\bBill Payment Successful\s+(?:₹|rs\.?)\s*[\d,.]+\s+For\s+([A-Za-z][\w .&-]{2,30}?)\s+(?:Customer|Consumer|Account)',
    r'purchased your\s+([A-Za-z][\w .&-]{2,40}?)\s+worth\b',
    r'\bto\s+VPA\s+(\S+@\S+?)\s+on\b',
    r'\bMerchant\s+([A-Za-z][\w .&*\'-]{2,40}?)(?:\s+(?:Transaction|Date|Time|Card)|[.,]|$)',
)]
CREDIT=re.compile(r'\b(credited|received|refund(?:ed)?|cashback|reversal|reversed)\b',re.I)
DEBIT=re.compile(r'\b(debited|spent|used for|paid|payment of|transaction of|purchase|towards|withdrawn|charged|thank you for using|money sent|sent successfully|fund transfer|receipt|total|transaction was successful|card transaction|travelling with us|thanks for riding|paid successfully)\b',re.I)
CARDBILL=re.compile(r'credit card (?:bill )?payment|bill payment .{0,30}credit card|payment received on your .{0,30}credit card|\bCRED (?:CLUB|CCBP)\b|towards CRED\b|DREAMPLUG',re.I)
TOPUP=re.compile(r'added (?:successfully )?(?:₹|rs\.?)?\s*[\d,.]*\s*(?:successfully )?to .{0,40}wallet|money added successfully|\brepayment\b|debit by transfer|hold for (?:rs|inr)|cash-load',re.I)

NOTSPEND=re.compile(r'^\s*(saved|off\b|discount|cashback up to|instant discount)',re.I)
PREFER=re.compile(r'(total|paid|amount|debited|spent|transaction of|for|charged|fare|payment of|bill)\W{0,3}(?:of\W{0,3})?$',re.I)
STATEMENT=re.compile(r'statement|minimum amount due|total amount due|payment due|wallet summary|balance summary|monthly summary',re.I)
def pick_amount(t):
    best=None
    for m in AMT.finditer(t):
        if NOTSPEND.search(t[m.end():m.end()+25]):continue          # "₹117 saved", "₹50 off"
        if BALANCE.search(t[max(0,m.start()-25):m.start()]):continue  # "Avl Bal INR 1,06,995": a balance, not the charge
        raw=m.group(2)
        v=float(raw.replace(',','.')) if re.fullmatch(r'\d+,\d{2}',raw) else float(raw.replace(',',''))   # "2593,59" = 2593.59
        if m.group(1).lower() in('usd','us$','$'):v=round(v*USD_INR,2)
        if v<=0:continue
        if PREFER.search(t[max(0,m.start()-30):m.start()]):return v,m   # "Order total ₹…", "debited Rs…"
        best=best or (v,m)
    return best
def parse(snippet,subject,typ=None):
    t=html.unescape(snippet or '')
    got=pick_amount(t) or pick_amount(subject or '')
    if not got:return None
    amt,m=got
    cur='USD' if m.group(1).lower() in('usd','us$','$') else 'INR'
    both=(subject or '')+' '+t
    if re.search(r'\b(failed|declined|unsuccessful|reversed)\b',both,re.I) and not re.search(r'refund',both,re.I):direction='failed'
    elif re.search(r'\b(buy|sell) order\b|order (?:is )?filled',both,re.I):direction='invest'
    elif re.search(r'zerodha|groww|upstox|indmoney|vested|fyers|dhan\b|kotak securities|angel one|coin by zerodha|smallcase|wazirx|binance|coindcx',both,re.I) and not re.search(r'brokerage|subscription|charges',both,re.I):direction='invest'   # money moved to brokers: not spending
    elif re.search(r'fund transfer|neft|rtgs|imps',both,re.I) and not re.search(r'\bat\b',t[:200]):direction='transfer'
    elif STATEMENT.search(subject or '') or STATEMENT.search(t[:120]):direction='statement'   # card statements: not a spend
    elif TOPUP.search(t) or CARDBILL.search(t) or CARDBILL.search(subject or '') or re.search(r'remittance|fund transfer to own|self transfer',(subject or '')+' '+t,re.I):direction='transfer'   # wallet top-up / paying the card bill: not new spending
    elif CREDIT.search(t) and not DEBIT.search(t[:m.start()]):direction='credit'
    elif DEBIT.search(t):direction='debit'
    else:direction='debit' if typ=='record' else 'unknown'                                # a receipt with an amount = a purchase
    merch=next((mm.group(1).strip(' .-') for p in MERCHANT for mm in [p.search(t)] if mm),'')
    merch=re.sub(r'[.\s]+(Total|Thanks?|Your).*$','',merch,flags=re.I)          # "Lucky Restaurant - Bandra West. Total"
    if not merch:   # fall back to the subject ("trip with Uber", "order from Mocha", "payment of ₹40 to Swiggy Dine In")
        sm=re.search(r'(?:trip with|order from|order with|payment of .{0,15}? to|receipt from|paid to)\s+([A-Za-z][\w .&\'-]{2,40}?)(?:\s+was\b|\s+is\b|$|[.,!])',subject or '',re.I)
        if sm:merch=sm.group(1).strip()
    ins=INSTR.search(t)
    return dict(amount=amt,direction=direction,merchant=merch[:60],instrument=ins.group(1) if ins else '',currency=cur)
BANKCATS=('Banking & cards','Investing & trading')

def main():
    con=db()
    con.execute('''create table if not exists ledger(id text primary key,date integer,amount real,direction text,merchant text,
        instrument text,sender text,subject text,type text,snippet text,parsed integer)''');con.commit()
    if '--csv' in sys.argv:
        f=sys.argv[sys.argv.index('--csv')+1]
        with open(f,'w',newline='') as fh:
            w=csv.writer(fh);w.writerow(['date','amount','direction','merchant','card_or_account','source','sender','subject','type','gmail_id'])
            for r in con.execute('select * from ledger where parsed=1 order by date'):
                w.writerow([time.strftime('%Y-%m-%d %H:%M',time.localtime(r['date'])),r['amount'],r['direction'],r['merchant'],r['instrument'],r['source'],r['sender'],r['subject'],r['type'],r['id']])
        print('wrote',f);return
    T={(r[0],r[1]):r[2] for r in con.execute('select sender,tmpl,type from mail_type')}
    have={r[0]:r[1] for r in con.execute('select id,snippet from ledger')}
    rows=[r for r in con.execute("select id,sender,subject,date from msg where state in('live','trashed')")
          if T.get((r['sender'],tmpl(r['subject']))) in('txn_alert','record')]
    if '--reparse' not in sys.argv:
        todo=[r for r in rows if r['id'] not in have]
        print(f'{len(rows)} alert/receipt emails, {len(todo)} previews to fetch',flush=True)
        g=service();i=0
        while i<len(todo):
            chunk=todo[i:i+50];got={};retry=[]
            def cb(rid,resp,exc):
                if exc is None:got[rid]=resp.get('snippet','')
                elif getattr(exc,'resp',None) is not None and exc.resp.status==404:got[rid]=''   # gone for good
                else:retry.append(rid)
            b=g.new_batch_http_request(callback=cb)
            for r in chunk:b.add(g.users().messages().get(userId='me',id=r['id'],format='minimal'),request_id=r['id'])
            b.execute()
            meta={r['id']:r for r in chunk}
            c=db();c.executemany('insert or replace into ledger(id,date,sender,subject,type,snippet,parsed) values(?,?,?,?,?,?,0)',
                [(k,meta[k]['date'],meta[k]['sender'],meta[k]['subject'],T.get((meta[k]['sender'],tmpl(meta[k]['subject']))),v) for k,v in got.items()]);c.commit()
            if retry:todo[i+50:i+50]=[meta[k] for k in retry];time.sleep(3)
            i+=50
            if (i//50)%40==0:print(f'{min(i,len(todo))}/{len(todo)}',flush=True)
            time.sleep(0.5)
    n,ok=reparse()
    print(f'DONE: {n} in ledger, {ok} with an amount')

# built-in spending categories: first match wins; your own rules (ledger_rule) override these
CATEGORY=[
 ('Food delivery',r'swiggy(?!.*instamart)|zomato|bundl|eatsure|uber eats|domino|faasos|box8|dunzo daily'),
 ('Groceries',r'instamart|blinkit|zepto|bigbasket|grofers|amazon pay in grocery|jiomart|dmart|milkbasket'),
 ('Transport',r'\buber\b|\bola\b|rapido|metro|irctc|redbus|fastag|petrol|fuel|hpcl|bpcl|indian oil|porter|namma yatri'),
 ('Travel',r'makemytrip|goibibo|indigo|air india|vistara|akasa|booking\.com|agoda|airbnb|oyo|cleartrip|yatra'),
 ('Subscriptions',r'claude|anthropic|openai|chatgpt|netflix|spotify|youtube|google play|apple\.com|icloud|prime video|hotstar|jiocinema|github|notion|kindle|runpod|aws|amazon web services|google cloud|digitalocean|vercel|cursor'),
 ('Bills & recharge',r'airtel|\bjio\b|vodafone|\bvi\b|bses|electricity|cspdcl|tata power|broadband|recharge|gas bill|water bill|act fibernet'),
 ('Insurance',r'insurance|bajaj ?allianz|lic\b|hdfc life|star health|policybazaar|acko|digit'),
 ('Shopping',r'amazon|flipkart|myntra|ajio|nykaa|meesho|shopify|tata cliq|croma|reliance digital|decathlon|ikea|snitch|razorpay'),
 ('Entertainment',r'bookmyshow|book my show|pvr|inox|steam|playstation|xbox|epic games|district'),
 ('Health',r'pharm|apollo|practo|1mg|netmeds|hospital|clinic|diagnostic|medplus'),
]
def category_for(text):
    for name,pat in CATEGORY:
        if re.search(pat,text,re.I):return name
    return ''

def rules(c):
    c.execute('''create table if not exists ledger_rule(id integer primary key,match text,field text,set_direction text,
        set_category text,rename text,created integer)''')
    return [dict(r) for r in c.execute('select * from ledger_rule order by id')]

def reparse():
    """Re-derive every ledger row from its stored preview: parser, then categories, then your rules (in order)."""
    c=db();n=ok=0
    cols=[x[1] for x in c.execute('pragma table_info(ledger)')]
    for col in('source','currency','category'):
        if col not in cols:c.execute(f'alter table ledger add column {col} text')
    cat={r[0]:r[1] for r in c.execute('select sender,category from sender_ai')}
    R=rules(c)
    c.execute('create table if not exists merchant_category(merchant text primary key,category text,source text,at integer)')
    MC={r[0]:r[1] for r in c.execute('select merchant,category from merchant_category')}   # local-AI answers
    for r in c.execute('select id,snippet,subject,type,sender from ledger').fetchall():
        p=parse(r['snippet'],r['subject'],r['type']);n+=1
        src='bank' if cat.get(r['sender']) in BANKCATS else 'merchant'   # bank alert vs shop receipt for the same spend
        if p and not p['merchant'] and src=='merchant':   # last resort: the shop's own name from its email domain
            dom=r['sender'].split('@')[-1].split('.');core=[x for x in dom if x not in('com','in','co','net','org','mail','email','e','m','info','noreply','mailer','updates')]
            p['merchant']=(core[-1] if core else '').capitalize()
        if not p:c.execute('update ledger set parsed=0 where id=?',(r['id'],));continue
        ok+=1
        p['category']=category_for(' '.join([p['merchant'],r['subject'] or '',r['sender']])) or MC.get(p['merchant'].upper().strip(),'')
        if not p['category'] and re.search(r'to VPA|UPI_|money sent|paid .* to',html.unescape(r['snippet'] or ''),re.I) and p['merchant'] and ' ' in p['merchant'] and p['merchant'].isupper():
            p['category']='People (UPI)'
        hay={'any':' '.join([p['merchant'],r['subject'] or '',html.unescape(r['snippet'] or ''),r['sender']]).lower(),'merchant':p['merchant'].lower()}
        for u in R:
            if u['match'] and u['match'].lower() in hay.get(u['field'] or 'any',hay['any']):
                if u['set_direction']:p['direction']=u['set_direction']
                if u['set_category']:p['category']=u['set_category']
                if u['rename']:p['merchant']=u['rename']
        c.execute('update ledger set amount=?,direction=?,merchant=?,instrument=?,source=?,currency=?,category=?,parsed=1 where id=?',
                  (p['amount'],p['direction'],p['merchant'],p['instrument'],src,p['currency'],p['category'] or 'Other',r['id']))
    c.commit()
    return n,ok

if __name__=='__main__':main()

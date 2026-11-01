"""Shared Gmail client + SQLite index for mail-swipe."""
import sqlite3,time,os,re
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
TOKEN=os.path.expanduser(os.environ.get('GMAIL_TOKEN','~/.config/google-storage-cleanup/gmail_token.json'))  # needs gmail.modify
DB=os.environ.get('MAILSWIPE_DB') or os.path.join(os.path.dirname(os.path.abspath(__file__)),'index.db')
def service():
    c=Credentials.from_authorized_user_file(TOKEN)
    if not c.valid:c.refresh(Request())
    return build('gmail','v1',credentials=c,cache_discovery=False)
def db():
    con=sqlite3.connect(DB,timeout=30);con.row_factory=sqlite3.Row
    con.execute('''create table if not exists msg(id text primary key,thread text,sender text,name text,subject text,
      date integer,size integer,labels text,unsub text,state text default 'live')''')
    con.execute('create index if not exists msg_sender on msg(sender)')
    if 'has_att' not in [c[1] for c in con.execute('pragma table_info(msg)')]:
        try:con.execute('alter table msg add column has_att integer default 0')
        except sqlite3.OperationalError:pass
    con.execute('create table if not exists settings(key text primary key,val text)')
    con.execute('create table if not exists sender_note(sender text primary key,decision text,at integer)')
    con.execute('create table if not exists action(id integer primary key,at integer,sender text,kind text,ids text)')
    if 'batch' not in [c[1] for c in con.execute('pragma table_info(action)')]:
        try:con.execute('alter table action add column batch integer')   # bulk actions share a batch id: one undo step
        except sqlite3.OperationalError:pass   # another thread added it first
    if 'notekey' not in [c[1] for c in con.execute('pragma table_info(action)')]:
        try:con.execute('alter table action add column notekey text')   # money-view decisions are remembered separately
        except sqlite3.OperationalError:pass
    con.execute('create table if not exists mail_type(sender text,tmpl text,type text,model text,at integer,primary key(sender,tmpl))')
    con.execute('create table if not exists pattern_override(sender text,tmpl text,prev_type text,prev_model text,at integer,primary key(sender,tmpl))')
    con.execute('''create table if not exists unsub_log(id integer primary key,sender text,at integer,method text,ok integer,
        detail text,response text,target text,list_unsub text,list_unsub_post text,msg text)''')
    con.execute('create table if not exists unsubs(sender text primary key,method text,status text,detail text,at integer)')
    con.execute('''create table if not exists sender_ai(sender text primary key,category text,subcategory text,action text,
        confidence real,reason text,n_at integer,model text,at integer)''')
    return con

def tmpl(subject):
    """Subject -> pattern: amounts, dates, account numbers and digits stripped, so 488 'transaction alert' mails share one key."""
    s=(subject or '').lower()
    s=re.sub(r'(rs\.?|inr|₹|usd|\$)\s*[\d,]+(\.\d+)?','<amt>',s)
    s=re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b','<mon>',s)
    s=re.sub(r'[x*]{2,}\d+','<acct>',s)
    s=re.sub(r'\d+','#',s)
    return re.sub(r'\s+',' ',s).strip()[:120]
MONEY_CATS=('Banking & cards','Investing & trading','Bills & utilities','Insurance','Tax & government','Security & accounts')
# type -> days to keep (None = keep forever)
TYPE_KEEP={'txn_alert':90,'otp_security':7,'marketing':0,'notice':365,'record':None}
def notekey(sender,cat):return 'money:'+sender if cat=='money' else sender

def mark_attachments(g,con):
    """Flag every message Gmail says has an attachment (has:attachment), so auto-clean never touches files."""
    ids=set();tok=None
    while True:
        r=g.users().messages().list(userId='me',q='has:attachment',maxResults=500,pageToken=tok,includeSpamTrash=False).execute()
        ids.update(m['id'] for m in r.get('messages',[]));tok=r.get('nextPageToken')
        if not tok:break
    con.execute('update msg set has_att=0')
    con.executemany('update msg set has_att=1 where id=?',[(i,) for i in ids]);con.commit()
    return len(ids)

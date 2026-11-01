#!/usr/bin/env python3
"""Index Gmail metadata (From/Subject/size/labels/List-Unsubscribe) into index.db. Re-runnable: fetches only new ids."""
import json,re,time,sys
from email.utils import parseaddr
from gmailapi import service,db,mark_attachments
g=service();con=db()
have={r[0] for r in con.execute('select id from msg')}
ids=[];tok=None
while True:
    r=g.users().messages().list(userId='me',maxResults=500,pageToken=tok,includeSpamTrash=False).execute()
    ids+=[m['id'] for m in r.get('messages',[])];tok=r.get('nextPageToken')
    if not tok:break
live=set(ids)
gone=have-live
con.executemany("update msg set state='gone' where id=? and state='live'",[(i,) for i in gone])
todo=[i for i in ids if i not in have]
print(len(ids),'in mailbox,',len(todo),'new to index',flush=True)
def row(m):
    h={x['name'].lower():x['value'] for x in m['payload'].get('headers',[])}
    name,addr=parseaddr(h.get('from',''))
    u=h.get('list-unsubscribe','');urls=re.findall(r'<([^>]+)>',u)
    unsub=next((x for x in urls if x.startswith('http')),urls[0] if urls else '')
    return (m['id'],m['threadId'],addr.lower(),name,h.get('subject',''),int(m['internalDate'])//1000,m['sizeEstimate'],
            ','.join(m.get('labelIds',[])),unsub)
i=0
while i<len(todo):
    chunk=todo[i:i+40];got=[];retry=[]
    def cb(rid,resp,exc):
        if exc is not None:retry.append(rid)
        else:got.append(row(resp))
    b=g.new_batch_http_request(callback=cb)
    for m in chunk:b.add(g.users().messages().get(userId='me',id=m,format='metadata',
        metadataHeaders=['From','Subject','List-Unsubscribe']),request_id=m)
    b.execute()
    con.executemany('insert or replace into msg(id,thread,sender,name,subject,date,size,labels,unsub) values(?,?,?,?,?,?,?,?,?)',got);con.commit()
    if retry:
        todo[i+40:i+40]=retry;time.sleep(3)  # rate-limited: requeue
    i+=40
    if (i//40)%50==0:print(i,'/',len(todo),flush=True)
    time.sleep(0.6)
print('attachments flagged:',mark_attachments(g,con),flush=True)
print('DONE',con.execute("select count(*) from msg where state='live'").fetchone()[0],'indexed')

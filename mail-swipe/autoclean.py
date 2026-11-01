#!/usr/bin/env python3
"""Daily AI auto-clean through the running Mail Swipe server. Does nothing unless the
'Run automatically every day' switch is on in the 🧹 Auto panel (or --apply is given).
Same plan, queue and one-step Undo as the panel."""
import json,sys,urllib.request
B='http://127.0.0.1:8092'
p=json.load(urllib.request.urlopen(B+'/auto/plan',timeout=600))
print(f"plan: trash {p['trash_n']} emails {p['trash_size']/1e6:.0f} MB | keep {p['keep_n']}")
if p.get('daily') or '--apply' in sys.argv:
    r=json.load(urllib.request.urlopen(urllib.request.Request(B+'/auto/run',data=b'{}',method='POST'),timeout=600))
    print('queued',r)
else:print('daily switch is off: report only')

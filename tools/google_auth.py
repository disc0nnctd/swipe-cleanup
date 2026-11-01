#!/usr/bin/env python3
"""One-time Google sign-in for a new scope, reusing the OAuth client from an existing token.

  google_auth.py CLIENT_FROM_TOKEN.json OUT_TOKEN.json SCOPE [SCOPE...]

The client's registered redirect is http://localhost:8765/. If you sign in on another device, the final
redirect to localhost fails there: copy that failed URL and `curl` it on this machine to finish.
"""
import json,sys
from google_auth_oauthlib.flow import InstalledAppFlow
src,out,*scopes=sys.argv[1:]
t=json.load(open(src))
cfg={'installed':{'client_id':t['client_id'],'client_secret':t['client_secret'],'auth_uri':'https://accounts.google.com/o/oauth2/auth',
     'token_uri':t.get('token_uri','https://oauth2.googleapis.com/token'),'redirect_uris':['http://localhost:8765/']}}
flow=InstalledAppFlow.from_client_config(cfg,scopes)
creds=flow.run_local_server(port=8765,open_browser=False,prompt='consent select_account',
    authorization_prompt_message='SIGN-IN URL: {url}',success_message='Done, you can close this tab.')
open(out,'w').write(creds.to_json());print('saved',out,creds.scopes,flush=True)

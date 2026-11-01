#!/usr/bin/env python3
"""Move decided files into <root>/_sorted/<bucket>/<original folders>/ (reversible moves, nothing deleted).

  google-photos/  -> upload to Google Photos (one album per folder)
  google-drive/   -> stays in Google Drive (backup copy kept here)
  local-only/     -> removed from Drive later; this disk is the home
  delete/         -> holding area; emptied only after review
Run with --dry-run first to see counts and sizes.
"""
import json,os,sys,shutil,collections
BUCKET={'photos':'google-photos','drive':'google-drive','local':'local-only','delete':'delete'}
args=[a for a in sys.argv[1:] if not a.startswith('--')];dry='--dry-run' in sys.argv
root=args[0] if args else '/path/to/storage/gdrive-backup'
dec=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'decisions.json')))
n=collections.Counter();sz=collections.Counter()
for p,l in dec.items():
    s=os.path.join(root,p)
    if not os.path.exists(s):continue
    n[l]+=1;sz[l]+=os.path.getsize(s)
    if not dry:
        t=os.path.join(root,'_sorted',BUCKET[l],p);os.makedirs(os.path.dirname(t),exist_ok=True);shutil.move(s,t)
for l in BUCKET:print(f'{BUCKET[l]:14} {n[l]:6} files {sz[l]/1e6:9.1f} MB')
print('DRY RUN, nothing moved' if dry else 'moved')

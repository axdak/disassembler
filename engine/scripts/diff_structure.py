#!/usr/bin/env python3
"""生成故事结构 before/after 差异报告。"""
from __future__ import annotations
import json, os, sys
from typing import Any, Dict, List
from story_schema_rules import COLLECTION_KEYS

def load(p):
    with open(p, 'r', encoding='utf-8') as f: return json.load(f)

def by_name(items):
    return {str(x.get('名称')): x for x in items or [] if isinstance(x, dict) and x.get('名称')}

def item_diff(a,b):
    d={}
    for k in sorted(set(a)|set(b)):
        if a.get(k)!=b.get(k): d[k]={"before":a.get(k),"after":b.get(k)}
    return d

def diff(before, after):
    out={"介绍":{},"元素集":{}}
    if before.get('介绍')!=after.get('介绍'): out['介绍']={"before":before.get('介绍'),"after":after.get('介绍')}
    for key in COLLECTION_KEYS:
        b=by_name(before.get(key,[])); a=by_name(after.get(key,[]))
        added=sorted(set(a)-set(b)); deleted=sorted(set(b)-set(a)); modified={}
        for name in sorted(set(a)&set(b)):
            d=item_diff(b[name],a[name])
            if d: modified[name]=d
        out['元素集'][key]={"新增":added,"删除":deleted,"修改":modified,"新增数量":len(added),"删除数量":len(deleted),"修改数量":len(modified)}
    return out

def main():
    if len(sys.argv)!=4:
        print('用法: python diff_structure.py <before.json> <after.json> <out.json>'); return 1
    report=diff(load(sys.argv[1]), load(sys.argv[2]))
    os.makedirs(os.path.dirname(os.path.abspath(sys.argv[3])), exist_ok=True)
    with open(sys.argv[3],'w',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
    print(f"差异报告已写入: {sys.argv[3]}")
    return 0
if __name__=='__main__': sys.exit(main())

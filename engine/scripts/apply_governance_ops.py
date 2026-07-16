#!/usr/bin/env python3
"""执行治理补丁中的确定性操作：合并、重命名、替换引用、删除、降级、设置字段。"""
from __future__ import annotations
import argparse, copy, json, os, shutil, sys, datetime as dt, tempfile
from typing import Any, Dict, List, Set
from story_schema_rules import (
    COLLECTION_KEYS, TYPE_TO_COLLECTION, DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS,
    dump_supplementary_tags, is_cumulative_detail_field, parse_supplementary_tags,
)

CUMULATIVE_DETAIL_ARRAY_FIELDS=DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_stdio()


def load(p):
    with open(p,'r',encoding='utf-8') as f: return json.load(f)
def save(p,d):
    directory=os.path.dirname(os.path.abspath(p))
    os.makedirs(directory,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f'.{os.path.basename(p)}.',suffix='.tmp',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(d,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
def names(item):
    s=set()
    if isinstance(item.get('名称'),str): s.add(item['名称'])
    for a in item.get('别名',[]) or []:
        if isinstance(a,str): s.add(a)
    return {x for x in s if x}
def find_idx(data, typ, name):
    key=TYPE_TO_COLLECTION[typ]
    for i,it in enumerate(data.get(key,[]) or []):
        if isinstance(it,dict) and name in names(it): return i
    return -1
def append_unique(a,b):
    a=list(a or [])
    for x in b or []:
        if x not in a: a.append(x)
    return a
def parse_json_string_array_or_lines(value):
    if value in (None,'',[],{}): return []
    if isinstance(value,list): return [str(x).strip() for x in value if str(x).strip()]
    if not isinstance(value,str):
        text=str(value).strip()
        return [text] if text else []
    text=value.strip()
    if not text: return []
    try:
        parsed=json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(parsed,list): return [str(x).strip() for x in parsed if str(x).strip()]
    return [text]
def dump_json_string_array(items):
    return json.dumps(append_unique([],items),ensure_ascii=False,separators=(',',':'))
def append_detail_value(existing, incoming, detail_key=''):
    if incoming in (None,'',[],{}):
        return existing
    if existing in (None,'',[],{}):
        if is_cumulative_detail_field(detail_key):
            return dump_json_string_array(parse_json_string_array_or_lines(incoming))
        return incoming
    if is_cumulative_detail_field(detail_key):
        return dump_json_string_array(parse_json_string_array_or_lines(existing)+parse_json_string_array_or_lines(incoming))
    if isinstance(existing,str) and isinstance(incoming,str):
        old=existing.strip()
        additions=[line.strip() for line in incoming.splitlines() if line.strip() and line.strip() not in old]
        return old if not additions else old+'\n'+'\n'.join(additions)
    if isinstance(existing,list) and isinstance(incoming,list):
        return append_unique(existing,incoming)
    return incoming
def replace_refs(obj, typ, old: Set[str], new: str):
    count=0
    def repl(v):
        nonlocal count
        if isinstance(v,str):
            if v in old:
                count+=1; return new
            if ':' in v:
                p,n=v.split(':',1)
                if p==typ and n in old:
                    count+=1; return f'{p}:{new}'
        return v
    def walk(x):
        if isinstance(x,dict):
            for k,v in list(x.items()):
                if isinstance(v,(dict,list)): walk(v)
                else: x[k]=repl(v)
        elif isinstance(x,list):
            newlist=[]
            for v in x:
                if isinstance(v,(dict,list)):
                    walk(v); nv=v
                else: nv=repl(v)
                if nv not in newlist: newlist.append(nv)
            x[:]=newlist
    walk(obj); return count
def remove_refs(obj, typ, old: Set[str]):
    count=0
    def should(v):
        if isinstance(v,str):
            if v in old: return True
            if ':' in v:
                p,n=v.split(':',1)
                return p==typ and n in old
        return False
    def walk(x):
        nonlocal count
        if isinstance(x,dict):
            for k,v in list(x.items()):
                if isinstance(v,list): walk(v)
                elif isinstance(v,dict): walk(v)
                elif should(v): x[k]=''; count+=1
        elif isinstance(x,list):
            kept=[]
            for v in x:
                if should(v): count+=1
                else:
                    if isinstance(v,(dict,list)): walk(v)
                    kept.append(v)
            x[:]=kept
    walk(obj); return count

def merge_items(target, source):
    for k,v in source.items():
        if k=='名称':
            continue
        if k in {'别名','标签集','关系','所属阵营','参与成员','目标事件','涉及事件'}:
            vals=v if isinstance(v,list) else []
            target[k]=append_unique(target.get(k,[]), vals)
        elif k=='详情' and isinstance(v,dict):
            target.setdefault('详情',{})
            if not isinstance(target['详情'],dict): target['详情']={}
            for dk,dv in v.items():
                target['详情'][dk]=append_detail_value(target['详情'].get(dk),dv,dk)
        elif k=='介绍' and isinstance(v,str) and v.strip():
            if not target.get(k): target[k]=v
            elif v not in target[k]: target[k]+='\n'+v
        else:
            if target.get(k) in (None,'',[],{}) and v not in (None,'',[],{}): target[k]=v

def apply_ops(data, patch):
    logs=[]; warns=[]; ops=patch.get('治理操作',{}) if isinstance(patch,dict) else {}
    if not isinstance(ops,dict): return data,logs,['治理操作不是对象']
    # 合并
    for op in ops.get('合并元素',[]) or []:
        try:
            typ=op['类型']; main=op.get('主') or op.get('保留'); sources=op.get('并入') or []
            if isinstance(sources,str): sources=[sources]
            key=TYPE_TO_COLLECTION[typ]; mi=find_idx(data,typ,main)
            if mi<0: raise ValueError(f'找不到主元素{typ}:{main}')
            target=data[key][mi]
            for s in sources:
                si=find_idx(data,typ,s)
                if si<0: warns.append(f'找不到待并入{typ}:{s}'); continue
                src=data[key][si]; old=names(src); merge_items(target,src)
                target['别名']=append_unique(target.get('别名',[]), list(old-{target.get('名称')}))
                c=replace_refs(data,typ,old,target['名称'])
                for idx,candidate in enumerate(data[key]):
                    if candidate is src and candidate is not target:
                        data[key].pop(idx)
                        break
                logs.append(f'合并{typ}:{s} -> {target["名称"]}，替换引用{c}处')
        except Exception as e: warns.append(f'合并元素失败: {e}')
    # 重命名
    for op in ops.get('重命名元素',[]) or []:
        try:
            typ=op['类型']; old=op['原名称']; new=op['新名称']; key=TYPE_TO_COLLECTION[typ]; idx=find_idx(data,typ,old)
            if idx<0: raise ValueError(f'找不到{typ}:{old}')
            item=data[key][idx]; oldnames=names(item)|{old}; item['名称']=new; item['别名']=append_unique(item.get('别名',[]), [old])
            c=replace_refs(data,typ,oldnames,new); logs.append(f'重命名{typ}:{old}->{new}，替换{c}处')
        except Exception as e: warns.append(f'重命名失败: {e}')
    # 替换引用
    for op in ops.get('替换引用',[]) or []:
        try:
            typ=op.get('类型') or op.get('原类型'); old=op['原名称']; new=op['新名称']; c=replace_refs(data,typ,{old},new); logs.append(f'替换引用{typ}:{old}->{new} {c}处')
        except Exception as e: warns.append(f'替换引用失败: {e}')
    # 设置字段/追加详情
    for op in ops.get('设置字段',[]) or []:
        try:
            idx=find_idx(data,op['类型'],op['名称']); key=TYPE_TO_COLLECTION[op['类型']]
            if idx<0: raise ValueError('找不到元素')
            data[key][idx][op['字段']]=op.get('值'); logs.append(f"设置字段 {op['类型']}:{op['名称']}.{op['字段']}")
        except Exception as e: warns.append(f'设置字段失败: {e}')
    for op in ops.get('追加详情',[]) or []:
        try:
            idx=find_idx(data,op['类型'],op['名称']); key=TYPE_TO_COLLECTION[op['类型']]
            if idx<0: raise ValueError('找不到元素')
            item=data[key][idx]; item.setdefault('详情',{})
            detail_key=op.get('字段') or op.get('详情键')
            item['详情'][detail_key]=append_detail_value(item['详情'].get(detail_key),op.get('值',''),detail_key); logs.append(f"追加详情 {op['类型']}:{op['名称']}")
        except Exception as e: warns.append(f'追加详情失败: {e}')
    # 标签治理：按补丁明确列出的降级项迁入可逆详情字段。
    for op in ops.get('治理标签',[]) or []:
        try:
            typ=op['类型']; name=op['名称']; key=TYPE_TO_COLLECTION[typ]; idx=find_idx(data,typ,name)
            if idx<0: raise ValueError(f'找不到元素{typ}:{name}')
            item=data[key][idx]
            retained=op['保留标签']; demoted=op['降级标签']; reason=op['理由']
            if not isinstance(reason,str) or not reason.strip(): raise ValueError('理由必须是非空字符串')
            if not isinstance(retained,list) or not isinstance(demoted,list): raise ValueError('保留标签和降级标签必须是数组')
            if any(not isinstance(tag,str) or not tag.strip() for tag in retained+demoted): raise ValueError('标签必须是非空字符串')
            retained=[tag.strip() for tag in retained]; demoted=[tag.strip() for tag in demoted]
            if len(set(retained))!=len(retained) or len(set(demoted))!=len(demoted): raise ValueError('保留标签和降级标签不得重复')
            if set(retained)&set(demoted): raise ValueError('保留标签和降级标签不得重叠')
            current=item.get('标签集')
            if not isinstance(current,list) or any(not isinstance(tag,str) or not tag.strip() for tag in current): raise ValueError('目标元素标签集无效')
            current=[tag.strip() for tag in current]
            if len(set(current))!=len(current): raise ValueError('目标元素标签集重复，需先规范化')
            if set(current)!=set(retained)|set(demoted): raise ValueError('保留标签与降级标签未完整覆盖治理前标签集')
            item.setdefault('详情',{})
            if not isinstance(item['详情'],dict): raise ValueError('目标元素详情必须是对象')
            existing=item['详情'].get('补充标签')
            existing_tags=[] if existing is None else parse_supplementary_tags(existing)
            merged=append_unique(existing_tags,demoted)
            item['标签集']=[tag for tag in current if tag not in set(demoted)]
            item['详情']['补充标签']=dump_supplementary_tags(merged)
            logs.append(f'治理标签 {typ}:{item["名称"]}，降级{len(demoted)}项；理由:{reason.strip()}')
        except Exception as e: warns.append(f'治理标签失败: {e}')
    # 降级/删除
    for bucket, degrade in [('降级元素',True),('删除元素',False)]:
        for op in ops.get(bucket,[]) or []:
            try:
                typ=op['类型']; name=op['名称']; key=TYPE_TO_COLLECTION[typ]; idx=find_idx(data,typ,name)
                if idx<0: warns.append(f'找不到{bucket}{typ}:{name}'); continue
                item=data[key].pop(idx); old=names(item)|{name}; c=remove_refs(data,typ,old)
                if degrade and op.get('挂载到'):
                    m=op['挂载到']; midx=find_idx(data,m['类型'],m['名称'])
                    if midx>=0:
                        mk=TYPE_TO_COLLECTION[m['类型']]; host=data[mk][midx]; host.setdefault('详情',{})
                        detail_key=m.get('详情键','降级元素')
                        detail_value=f"【{typ}】{item.get('名称',name)}；原介绍:{item.get('介绍','')}；原因:{op.get('原因','')}"
                        host['详情'][detail_key]=append_detail_value(host['详情'].get(detail_key),detail_value,detail_key)
                logs.append(f'{bucket}{typ}:{name}，移除引用{c}处')
            except Exception as e: warns.append(f'{bucket}失败: {e}')
    return data,logs,warns

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('story_json'); ap.add_argument('patch_json'); ap.add_argument('--out',default=''); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--backup',action='store_true')
    args=ap.parse_args(); data=load(args.story_json); patch=load(args.patch_json); new,logs,warns=apply_ops(copy.deepcopy(data),patch)
    print('=== 治理操作执行结果 ===')
    for l in logs: print('  - '+l)
    for w in warns: print('  ! '+w)
    if args.dry_run: return 0 if not warns else 2
    out=args.out or args.story_json
    if args.backup and out==args.story_json: shutil.copy2(args.story_json,args.story_json+'.bak_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S'))
    save(out,new); print(f'已写入: {out}')
    return 0 if not warns else 2
if __name__=='__main__': sys.exit(main())

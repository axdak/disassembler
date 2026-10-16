#!/usr/bin/env python3
"""执行治理补丁中的确定性操作：合并、重命名、替换引用、删除、降级、设置字段。"""
from __future__ import annotations
import argparse, copy, json, os, shutil, sys, datetime as dt, tempfile
from typing import Any, Dict, List, Set
from element_identity import build_lookup_index, find_index
from chronology import parse_chapter_numbers, format_chapter
from information_preservation import (ARCHIVE_FIELD, CORRECTION_FIELD, append_record, reference_context,
                                      evidenced_correction, is_trace_field_correction, set_field_operation_errors)
from story_schema_rules import (
    COLLECTION_KEYS, TYPE_TO_COLLECTION, STRUCTURAL_REF_FIELDS, DETAIL_CUMULATIVE_JSON_ARRAY_FIELDS, TRACEABLE_COLLECTION_KEYS,
    dump_supplementary_tags, is_cumulative_detail_field, merge_supplementary_tag_values, is_event_compound_tag,
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
    """Use the same canonical-name-first rules as ordinary chapter merging."""
    return find_index(*build_lookup_index(data.get(TYPE_TO_COLLECTION[typ], []) or []), name)


def source_names(data, typ, item):
    """Only rewrite identities that currently resolve to this source object."""
    items = data.get(TYPE_TO_COLLECTION[typ], [])
    resolved = set()
    for name in names(item):
        try:
            index = find_idx(data, typ, name)
        except ValueError:
            # Shared aliases cannot identify a source. Keep them as metadata;
            # after an explicit canonical merge they may become unambiguous.
            continue
        if index >= 0 and items[index] is item:
            resolved.add(name)
    return resolved

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
    if detail_key == '补充标签':
        # Validate strictly while retaining raw evidence whitespace across appends.
        return merge_supplementary_tag_values(existing, incoming)
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
def rewrite_refs(data, typ, old, new):
    """Rewrite typed references only; names, aliases and narrative text are data.

    Structural reference fields have a known target collection. Free-form
    details are changed only for explicit type:name values, never substrings.
    A None replacement removes the reference, preserving unrelated content.
    """
    count = 0
    target = TYPE_TO_COLLECTION[typ]

    def replace(value, relation=False, explicit=False):
        nonlocal count
        if not isinstance(value, str):
            return value
        prefix, separator, name = value.partition(':')
        match = (separator and name in old and (relation or TYPE_TO_COLLECTION.get(prefix) == target)) if (relation or explicit) else value in old
        if not match:
            return value
        count += 1
        return None if new is None else f'{prefix}:{new}' if (relation or explicit) else new

    for collection, items in data.items():
        if collection not in COLLECTION_KEYS or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field, field_target, kind in STRUCTURAL_REF_FIELDS[collection]:
                if field_target != target or field not in item:
                    continue
                value = item[field]
                if kind == 'scalar':
                    item[field] = replace(value) or ''
                elif isinstance(value, list):
                    item[field] = append_unique([], [result for entry in value
                        if (result := replace(entry, relation=kind == 'relation_list')) is not None])
            details = item.get('详情')
            if isinstance(details, dict):
                for field, value in details.items():
                    if isinstance(value, str):
                        details[field] = replace(value, explicit=True) or ''
                    elif isinstance(value, list):
                        details[field] = append_unique([], [result for entry in value
                            if (result := replace(entry, explicit=True)) is not None])
    return count


def replace_refs(obj, typ, old: Set[str], new: str):
    """Redirect references without renaming any element declaration."""
    return rewrite_refs(obj, typ, old, new)


def remove_refs(obj, typ, old: Set[str]):
    """Remove references to an explicitly deleted element only."""
    return rewrite_refs(obj, typ, old, None)


def archive_item(detail, source, **context):
    """已有档案历史平铺一次；纠错记录随所属原档案保存，保留身份归属。"""
    snapshot = copy.deepcopy(source)
    original_detail = snapshot.get('详情', {})
    if isinstance(original_detail, dict) and ARCHIVE_FIELD in original_detail:
        detail[ARCHIVE_FIELD] = append_detail_value(detail.get(ARCHIVE_FIELD), original_detail.pop(ARCHIVE_FIELD), ARCHIVE_FIELD)
    append_record(detail, ARCHIVE_FIELD, {**context, "原档案": snapshot})


def merge_items(target, source):
    # 来源档案完整留存；非空标量冲突不能因身份合并而静默抹去。
    archive_item(target.setdefault('详情', {}), source, 操作="合并")
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
                # 来源归档已平铺迁移，来源纠错则属于上方保存的来源快照，不能变成主档案纠错。
                if dk in {ARCHIVE_FIELD, CORRECTION_FIELD}:
                    continue
                if dk in {'首次章节', '最近章节'}:
                    # Chronology metadata is a range, not an append-only paragraph.
                    numbers = parse_chapter_numbers(target['详情'].get(dk)) + parse_chapter_numbers(dv)
                    if numbers:
                        target['详情'][dk] = format_chapter(min(numbers) if dk == '首次章节' else max(numbers))
                    else:
                        # Invalid chronology must reach validation, not vanish.
                        target['详情'][dk] = append_detail_value(target['详情'].get(dk), dv, dk)
                else:
                    target['详情'][dk]=append_detail_value(target['详情'].get(dk),dv,dk)
        elif k=='介绍' and isinstance(v,str) and v.strip():
            if not target.get(k): target[k]=v
            elif v not in target[k]: target[k]+='\n'+v
        else:
            if target.get(k) in (None,'',[],{}) and v not in (None,'',[],{}):
                target[k]=v
            elif target.get(k) != v and v not in (None,'',[],{}):
                append_record(target['详情'], '待确认信息', {"事项": "合并字段冲突", "字段": k,
                              "保留值": target.get(k), "来源值": v, "来源名称": source.get('名称')})

def apply_ops(data, patch):
    # Reject protected-tag demotions before any other operation can run. This
    # execution-layer guard also covers callers that bypass Delta validation;
    # four-dimensional formal event semantics must not become mere details.
    proposed_ops = patch.get('治理操作', {}) if isinstance(patch, dict) else {}
    if isinstance(proposed_ops, dict) and isinstance(proposed_ops.get('治理标签', []), list):
        for op in proposed_ops.get('治理标签', []):
            if (isinstance(op, dict) and op.get('类型') == '事件'
                    and isinstance(op.get('降级标签'), list)
                    and any(is_event_compound_tag(tag) for tag in op['降级标签'])):
                raise ValueError(f"治理标签[事件:{op.get('名称')}]禁止降级有效四维复合标签；应保留在标签集")
    original = copy.deepcopy(data)  # Any invalid operation aborts the entire patch.
    data = copy.deepcopy(data)
    # 对象身份跨本补丁的重命名保持不变；旧值始终来自操作前快照。
    preimages = {id(item): original[key][index] for key in COLLECTION_KEYS
                 for index, item in enumerate(data.get(key, [])) if isinstance(item, dict)}
    merged_trace_values = {}
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
                src=data[key][si]
                if src is target:
                    raise ValueError(f'禁止自合并{typ}:{s}')
                old=source_names(data,typ,src)
                merge_items(target,src)
                if key in TRACEABLE_COLLECTION_KEYS:
                    # 仅记录本补丁真正执行的合并结果，不把“目标值相等”当作忽略原值的许可。
                    merged_trace_values[id(target)] = {field: copy.deepcopy(target['详情'].get(field))
                                                      for field in ('首次章节', '最近章节')}
                target['别名']=append_unique(target.get('别名',[]), sorted(old-{target.get('名称')}))
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
            other=find_idx(data,typ,new)
            if other >= 0 and other != idx: raise ValueError(f'重命名目标已存在{typ}:{new}')
            item=data[key][idx]; oldnames=source_names(data,typ,item)|{old}; item['名称']=new; item['别名']=append_unique(item.get('别名',[]), [old])
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
            boundary_errors = set_field_operation_errors(op, patch)
            if boundary_errors:
                raise ValueError('；'.join(boundary_errors))
            idx=find_idx(data,op['类型'],op['名称']); key=TYPE_TO_COLLECTION[op['类型']]
            if idx<0: raise ValueError('找不到元素')
            item=data[key][idx]; field=op['字段']; owner=item; leaf=field
            if field.startswith('详情/'):
                leaf=field.removeprefix('详情/'); owner=item.setdefault('详情', {})
            old=copy.deepcopy(owner.get(leaf))
            snapshot_detail = preimages.get(id(item), {}).get('详情', {})
            merge_value = merged_trace_values.get(id(item), {}).get(leaf)
            merge_satisfied = (is_trace_field_correction(op) and isinstance(snapshot_detail, dict)
                               and leaf in snapshot_detail and op.get('原值') == snapshot_detail[leaf]
                               and snapshot_detail[leaf] != merge_value and old == merge_value == op.get('值'))
            if '原值' in op and op['原值'] != old and not merge_satisfied:
                raise ValueError('纠错原值已经变化，拒绝覆盖')
            archive_old = copy.deepcopy(snapshot_detail[leaf]) if merge_satisfied else old
            if owner is not item and not evidenced_correction(op, archive_old, op.get('值')):
                raise ValueError('详情纠错必须声明原值、理由和原文章节引文')
            if old != op.get('值') or merge_satisfied:
                # 合并已达成目标时仍归档原主档案值，不能让合并过程抹掉纠错依据。
                append_record(item.setdefault('详情', {}), CORRECTION_FIELD,
                              {"字段": field, "原值": archive_old, "新值": op.get('值'),
                               "理由": op.get('理由', ''), "证据": op.get('证据', [])})
            owner[leaf]=op.get('值'); logs.append(f"设置字段 {op['类型']}:{op['名称']}.{field}")
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
            # 复用无损追加规则；严格校验失败时不修改主标签，整份补丁仍按原规则回滚。
            merged=merge_supplementary_tag_values(existing, dump_supplementary_tags(demoted) if demoted else '')
            item['标签集']=[tag for tag in current if tag not in set(demoted)]
            item['详情']['补充标签']=merged
            logs.append(f'治理标签 {typ}:{item["名称"]}，降级{len(demoted)}项；理由:{reason.strip()}')
        except Exception as e: warns.append(f'治理标签失败: {e}')
    # 降级/删除
    for bucket, degrade in [('降级元素',True),('删除元素',False)]:
        for op in ops.get(bucket,[]) or []:
            try:
                typ=op['类型']; name=op['名称']; key=TYPE_TO_COLLECTION[typ]; idx=find_idx(data,typ,name)
                if idx<0: warns.append(f'找不到{bucket}{typ}:{name}'); continue
                item=data[key][idx]; old=source_names(data,typ,item)|{name}
                host = None
                if degrade:
                    # 在任何移除发生前确认挂载，失败由整批回滚保护。
                    m=op.get('挂载到')
                    if not isinstance(m, dict): raise ValueError('降级必须指定有效挂载目标')
                    mk=TYPE_TO_COLLECTION[m['类型']]; midx=find_idx(data,m['类型'],m['名称'])
                    if midx<0: raise ValueError('降级挂载目标不存在')
                    host=data[mk][midx]
                    if host is item: raise ValueError('不能降级挂载到自身')
                    # 同批删除或降级挂载对象会使档案再次丢失，不能依赖执行顺序。
                    if any(candidate.get('类型') == m['类型'] and candidate.get('名称') in names(host)
                           for action in ('降级元素','删除元素') for candidate in ops.get(action,[]) or []):
                        raise ValueError('降级挂载目标不能在同批被移除')
                    archive_item(host.setdefault('详情', {}), item, 操作="降级", 原因=op.get('原因',''),
                                 原引用=reference_context(data, typ, old))
                data[key].pop(idx); c=remove_refs(data,typ,old)
                if degrade and op.get('挂载到'):
                    m=op['挂载到']; midx=find_idx(data,m['类型'],m['名称'])
                    if midx>=0:
                        mk=TYPE_TO_COLLECTION[m['类型']]; host=data[mk][midx]; host.setdefault('详情',{})
                        detail_key=m.get('详情键','降级元素')
                        detail_value=f"【{typ}】{item.get('名称',name)}；原介绍:{item.get('介绍','')}；原因:{op.get('原因','')}"
                        host['详情'][detail_key]=append_detail_value(host['详情'].get(detail_key),detail_value,detail_key)
                logs.append(f'{bucket}{typ}:{name}，移除引用{c}处')
            except Exception as e: warns.append(f'{bucket}失败: {e}')
    return (original, [], warns) if warns else (data, logs, warns)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('story_json'); ap.add_argument('patch_json'); ap.add_argument('--out',default=''); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--backup',action='store_true')
    args=ap.parse_args(); data=load(args.story_json); patch=load(args.patch_json); new,logs,warns=apply_ops(copy.deepcopy(data),patch)
    print('=== 治理操作执行结果 ===')
    for l in logs: print('  - '+l)
    for w in warns: print('  ! '+w)
    if warns:
        print('治理操作未完整通过，未写入任何修改。')
        return 1
    if args.dry_run: return 0
    out=args.out or args.story_json
    if args.backup and out==args.story_json:
        # 备份统一放到 源同级/故事结构版本/备份/，避免根目录被 .bak 堆积
        _bak_dir = os.path.join(os.path.dirname(os.path.abspath(args.story_json)), "故事结构版本", "备份")
        os.makedirs(_bak_dir, exist_ok=True)
        _bak = os.path.join(_bak_dir, os.path.basename(args.story_json)+'.bak_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S'))
        shutil.copy2(args.story_json, _bak)
        print(f'已备份: {_bak}')
    save(out,new); print(f'已写入: {out}')
    return 0 if not warns else 2
if __name__=='__main__': sys.exit(main())

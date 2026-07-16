#!/usr/bin/env python3
"""章节拆分脚本：支持自定义正则、前言处理、增强索引。"""
from __future__ import annotations
import argparse, json, os, re, sys, zipfile, xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple, Optional

# 强特征模式：含有“第/章/卷/序/尾声/番外”等明确标记，误报率低。
CHAPTER_PATTERNS_STRONG = [
    r'^[\s]*第[一二三四五六七八九十百千万零〇\d]+[章节回卷集部篇][\s　]*.*',
    r'^[\s]*[Cc]hapter[\s]+\d+.*',
    r'^[\s]*[卷回集部篇][一二三四五六七八九十百千万零〇\d]+.*',
    r'^[\s]*(?:序[章幕言]?|楔子|引[子言篇]|开篇)[\s　]*.*',
    r'^[\s]*(?:尾声|终[章篇]|结语|后记|结尾)[\s　]*.*',
    r'^[\s]*(?:番外|外传|特别[篇章]|附录|插话)[\s　\d]*.*',
]
# 弱特征模式：只在强模式全部失败时启用，避免“1. xxx”“【批注】”等行误切。
CHAPTER_PATTERNS_WEAK = [
    r'^[\s]*\d{1,5}[\s\.、．]+.*',
    r'^[\s]*【[^】]+】.*',
]
CHAPTER_PATTERNS = CHAPTER_PATTERNS_STRONG + CHAPTER_PATTERNS_WEAK
CN_NUM_MAP={'零':0,'〇':0,'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100,'千':1000,'万':10000}
ORDER_PATTERNS=[r'第(\d+)',r'[Cc]hapter\s+(\d+)',r'^\s*(\d{1,5})',r'第([一二三四五六七八九十百千万零〇]+)']

def cn_to_int(s:str)->int:
    if not s: return 0
    if s.isdigit(): return int(s)
    result=temp=0
    for ch in s:
        if ch in CN_NUM_MAP:
            val=CN_NUM_MAP[ch]
            if val>=10:
                if temp==0: temp=1
                result+=temp*val; temp=0
            else: temp=val
    return result+temp

def read_file(path:str)->str:
    ext=Path(path).suffix.lower()
    if ext=='.docx':
        with zipfile.ZipFile(path,'r') as zf:
            xml=zf.read('word/document.xml')
        root=ET.fromstring(xml)
        paras=[]
        for para in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
            texts=[n.text for n in para.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t') if n.text]
            if ''.join(texts).strip(): paras.append(''.join(texts))
        return '\n'.join(paras)
    for enc in ['utf-8','utf-8-sig','gb18030','gbk','big5','utf-16']:
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeError:
            pass
    raise ValueError(f'无法解码文件: {path}')

def detect_pattern(text:str, sample_lines:int=0, min_matches:int=2)->Tuple[Optional[str],List[str]]:
    # sample_lines<=0 表示扫全文；保留参数仅为向后兼容。
    # 两阶段：先在强特征模式里选命中数最多的；强模式全失败再尝试弱模式。
    # 避免“1. xxx”“【批注】”等行抢在“第N章”之前匹配。
    lines=text.splitlines() if sample_lines<=0 else text.splitlines()[:sample_lines]
    stripped=[line.strip() for line in lines if line.strip()]
    def _best(pats:List[str])->Tuple[Optional[str],List[str]]:
        bp:Optional[str]=None; bm:List[str]=[]
        for pat in pats:
            matches=[s for s in stripped if re.match(pat,s)]
            if len(matches)>=min_matches and len(matches)>len(bm):
                bp,bm=pat,matches
        return bp,bm
    pat,matches=_best(CHAPTER_PATTERNS_STRONG)
    if pat:
        print(f"采用强特征模式: {pat} （命中 {len(matches)}）")
        return pat,matches
    pat,matches=_best(CHAPTER_PATTERNS_WEAK)
    if pat:
        print(f"WARNING: 仅匹配到弱特征模式: {pat} （命中 {len(matches)}），可能存在误切。")
        return pat,matches
    return None,[]

def extract_order(title:str)->int:
    for pat in ORDER_PATTERNS:
        m=re.search(pat,title)
        if m:
            return int(m.group(1)) if m.group(1).isdigit() else cn_to_int(m.group(1))
    return 0

def normalize_title(title:str)->str:
    # 尽量去掉“第X章”等前缀，不强制。
    t=re.sub(r'^\s*第[一二三四五六七八九十百千万零〇\d]+[章节回卷集部篇]\s*','',title).strip()
    t=re.sub(r'^\s*[Cc]hapter\s+\d+\s*','',t).strip()
    return t or title.strip()

def safe_name(s:str)->str:
    s=re.sub(r'[\\/:*?"<>|\s]+','_',s).strip('_')
    return s[:50] or '未命名'

def write_index(out_dir:Path,index:List[dict],source_file:str,pattern:str,preface_mode:str):
    data={"source_file":os.path.abspath(source_file),"pattern":pattern,"preface_mode":preface_mode,"chapter_count":sum(1 for x in index if not x.get('is_preface')),"chapters":index}
    (out_dir/'_索引.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"章节索引已写入: {out_dir/'_索引.json'}")

def split_novel(input_file:str, output_dir:str, pattern:str='', preface_mode:str='separate', sample_lines:int=0, min_matches:int=2):
    out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
    content=read_file(input_file)
    pat=pattern or detect_pattern(content,sample_lines,min_matches)[0]
    if not pat:
        print('WARNING: 无法检测章节标题模式，将整本书作为第001章输出。')
        fp=out/'第001章_全文.md'; fp.write_text('# 全文\n\n'+content,encoding='utf-8')
        idx=[{"seq":1,"source_order":1,"source_title":"全文","normalized_title":"全文","filename":fp.name,"title":"全文","is_preface":False}]
        write_index(out,idx,input_file,'unknown',preface_mode); return idx
    print(f"检测/使用章节模式: {pat}")
    lines=content.split('\n')
    chapters=[]; cur_title=None; cur=[]; pre=[]
    for line in lines:
        stripped=line.strip()
        if stripped and re.match(pat,stripped):
            if cur_title is None:
                if ''.join(cur).strip(): pre=cur[:]
            else:
                chapters.append({'title':cur_title,'content':'\n'.join(cur)})
            cur_title=stripped; cur=[]
        else:
            cur.append(line)
    if cur_title is not None:
        chapters.append({'title':cur_title,'content':'\n'.join(cur)})
    if pre and ''.join(pre).strip():
        if preface_mode=='attach' and chapters:
            chapters[0]['content']='\n'.join(pre).strip()+'\n\n'+chapters[0]['content']
        elif preface_mode=='separate':
            (out/'_前言.md').write_text('# 前言\n\n'+'\n'.join(pre).strip(),encoding='utf-8')
        elif preface_mode=='drop':
            pass
    index=[]; seq=1
    for i,ch in enumerate(chapters,1):
        filename=f"第{seq:03d}章_{safe_name(ch['title'])}.md"
        (out/filename).write_text(f"# {ch['title']}\n\n{ch['content']}",encoding='utf-8')
        index.append({"seq":seq,"source_order":extract_order(ch['title']) or i,"source_title":ch['title'],"normalized_title":normalize_title(ch['title']),"filename":filename,"title":ch['title'],"is_preface":False})
        seq+=1
    write_index(out,index,input_file,pat,preface_mode)
    print(f"拆分完成: {len(index)} 个章节文件。")
    return index

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('input_file'); ap.add_argument('output_dir')
    ap.add_argument('--pattern',default='')
    ap.add_argument('--preface-mode',choices=['separate','attach','drop'],default='separate')
    ap.add_argument('--sample-lines',type=int,default=0,help='0=扫全文（默认）；>0=只看前N行')
    ap.add_argument('--min-matches',type=int,default=2)
    args=ap.parse_args()
    split_novel(args.input_file,args.output_dir,args.pattern,args.preface_mode,args.sample_lines,args.min_matches)
    return 0
if __name__=='__main__': sys.exit(main())

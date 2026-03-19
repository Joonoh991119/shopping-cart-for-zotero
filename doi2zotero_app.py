#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║                  DOI-to-Zotero Auto-Crawler                     ║
║                     v2.0 (Web GUI)                              ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  개요 (Overview)                                                 ║
║  ──────────────                                                  ║
║  DOI 리스트를 입력하면 자동으로:                                 ║
║    1. CrossRef API에서 논문 메타데이터(제목, 저자, 저널 등) 해석 ║
║    2. 3단계 전략으로 PDF 다운로드 시도                           ║
║       - Unpaywall (Open Access 무료 PDF)                        ║
║       - Sci-Hub (미러 자동 탐색)                                ║
║       - Direct Publisher (citation_pdf_url + URL 패턴 매칭)     ║
║    3. Zotero 라이브러리에 item 생성 + PDF 첨부                  ║
║                                                                  ║
║  실행 방법                                                       ║
║  ──────────                                                      ║
║    1. Zotero를 완전히 종료                                       ║
║    2. python3 doi2zotero_app.py                                  ║
║    3. 브라우저가 자동으로 열림 (http://localhost:8765)           ║
║                                                                  ║
║  의존성: Python 3.8+, requests (pip install requests)            ║
║  GUI: 내장 HTTP 서버 + 브라우저 (tkinter 불필요)                ║
║                                                                  ║
║  Author: CSNL Lab (Seoul National University)                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import hashlib, json, os, random, re, shutil, sqlite3
import string, subprocess, sys, threading, time, urllib.parse, webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, List, Dict

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# ── 설정 ─────────────────────────────────────────────────────
PORT = 8765
DEFAULT_ZOTERO = str(Path.home() / "Zotero")
EMAIL = "vnilab@gmail.com"
SCIHUB = ["https://sci-hub.kr", "https://sci-hub.ru", "https://sci-hub.se", "https://sci-hub.st"]
TIMEOUT = 30
DELAY = 2

# ── 글로벌 상태 (로그, 진행률을 브라우저에 스트리밍) ─────────
LOG = []          # [{"ts": "14:30:01", "msg": "...", "tag": "ok|fail|skip|info"}, ...]
PROGRESS = {"current": 0, "total": 0, "running": False, "done": False,
            "ok": 0, "fail": 0, "skip": 0}

def _log(msg, tag="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    LOG.append({"ts": ts, "msg": msg, "tag": tag})

# ── 데이터 클래스 ────────────────────────────────────────────
@dataclass
class PaperMeta:
    doi: str; title: str = ""; authors: list = field(default_factory=list)
    date: str = ""; journal: str = ""; volume: str = ""; issue: str = ""
    pages: str = ""; abstract: str = ""; url: str = ""
    item_type: str = "journalArticle"; publisher: str = ""

# ── DOI 파싱 ─────────────────────────────────────────────────
def parse_dois(text):
    dois = re.compile(r'10\.\d{4,9}/[^\s,;"\'\]}>]+').findall(text)
    seen, out = set(), []
    for d in dois:
        d = d.rstrip(".")
        if d not in seen: seen.add(d); out.append(d)
    return out

# ── CrossRef 메타데이터 ──────────────────────────────────────
def fetch_meta(doi):
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    try:
        r = requests.get(url, headers={"User-Agent": f"doi2zotero/2.0 (mailto:{EMAIL})"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return PaperMeta(doi=doi, title=doi, url=f"https://doi.org/{doi}")
        d = r.json()["message"]
    except Exception:
        return PaperMeta(doi=doi, title=doi, url=f"https://doi.org/{doi}")
    authors = [{"firstName": a.get("given",""), "lastName": a.get("family",""),
                "creatorType": "author"} for a in d.get("author", [])]
    dp = d.get("published-print", d.get("published-online", {})).get("date-parts", [[]])[0]
    date_str = "-".join(str(x) for x in dp) if dp else ""
    titles = d.get("title", []); title = titles[0] if titles else doi
    ct = d.get("type", "")
    it = ("bookSection" if "book-chapter" in ct else "conferencePaper" if "proceedings" in ct
          else "book" if "book" in ct and "chapter" not in ct else "journalArticle")
    jn = d.get("container-title", [])
    ab = re.sub(r'<[^>]+>', '', d.get("abstract", ""))
    return PaperMeta(doi=doi, title=title, authors=authors, date=date_str,
        journal=jn[0] if jn else "", volume=d.get("volume",""), issue=d.get("issue",""),
        pages=d.get("page",""), abstract=ab, url=f"https://doi.org/{doi}",
        item_type=it, publisher=d.get("publisher",""))

# ── PDF 다운로드 엔진 ────────────────────────────────────────
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.7"})

def _ok_pdf(p):
    if not p.exists() or p.stat().st_size < 5000: return False
    with open(p,"rb") as f: return f.read(4)==b"%PDF"

def _dl(url, dest):
    try:
        r = S.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code != 200: return False
        ct = r.headers.get("Content-Type","")
        if "html" in ct.lower() and "pdf" not in ct.lower(): return False
        with open(dest,"wb") as f:
            for ch in r.iter_content(8192): f.write(ch)
        return _ok_pdf(dest)
    except: return False

def _unpaywall(doi, dest):
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi,safe='')}?email={EMAIL}", timeout=TIMEOUT)
        if r.status_code != 200: return False
        data = r.json()
        # Try ALL OA locations — repositories (PMC) often work when publishers block bots
        for loc in data.get("oa_locations", []):
            u = loc.get("url_for_pdf") or loc.get("url")
            if u and _dl(u, dest): return True
        return False
    except: return False

def _crossref_links(doi, dest):
    """Try PDF links from CrossRef metadata — works for MIT Press, some others."""
    try:
        r = requests.get(f"https://api.crossref.org/works/{urllib.parse.quote(doi,safe='')}",
                        headers={"User-Agent": f"doi2zotero/2.0 (mailto:{EMAIL})"}, timeout=TIMEOUT)
        if r.status_code != 200: return False
        links = r.json().get("message", {}).get("link", [])
        for link in links:
            url = link.get("URL", "")
            ct = link.get("content-type", "")
            if ("pdf" in ct.lower() or "unspecified" in ct.lower()) and _dl(url, dest): return True
        return False
    except: return False

def _europepmc(doi, dest):
    """EuropePMC fallback — find PMCID, download via render endpoint."""
    try:
        r = requests.get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json&resultType=core", timeout=TIMEOUT)
        if r.status_code != 200: return False
        results = r.json().get("resultList", {}).get("result", [])
        if not results: return False
        pmcid = results[0].get("pmcid", "")
        if not pmcid: return False
        if _dl(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf", dest): return True
        return _dl(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/", dest)
    except: return False

# ── Sci-Hub session (separate from main session to avoid 403) ──
_SH = requests.Session()
_SH.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"})

def _scihub(doi, dest):
    for m in SCIHUB:
        try:
            r = _SH.get(f"{m}/{doi}", timeout=TIMEOUT)
            if r.status_code != 200: continue
            # Extended patterns: object data, citation_pdf_url, iframe/embed, direct link
            for pat in [r'<object[^>]*data\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']',
                        r'citation_pdf_url["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
                        r'(?:iframe|embed)[^>]*src\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']',
                        r'(https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*)']:
                x = re.search(pat, r.text, re.I)
                if not x: continue
                u = x.group(1).split("#")[0]  # Remove URL fragment
                if u.startswith("//"): u = "https:" + u
                elif u.startswith("/"): u = m + u
                if _dl(u, dest): return True
        except: continue
    return False

def _pub_patterns(url, doi):
    ps, u = [], url.lower()
    if "sciencedirect.com" in u:
        pii = re.search(r'/pii/(\w+)', u)
        if pii: ps.append(f"https://www.sciencedirect.com/science/article/pii/{pii.group(1)}/pdfft")
    if "springer.com" in u or "nature.com" in u:
        ps.append(url.replace("/article/","/content/pdf/")+".pdf")
    if "wiley.com" in u:
        ps.append(url.replace("/abs/","/pdfdirect/")+"?download=true")
    if "plos" in u:
        x = re.search(r'article\?id=([\d.]+/[\w.]+)', u)
        if x: ps.append(f"https://journals.plos.org/plosone/article/file?id={x.group(1)}&type=printable")
    if "pnas.org" in u:
        ps.append(url.replace("/doi/full/","/doi/pdf/"))
        ps.append(url.replace("/doi/abs/","/doi/pdf/"))
        ps.append(url.rstrip("/")+".full.pdf")
    if "academic.oup.com" in u:
        ps.append(url.rstrip("/")+".full.pdf")
    if "royalsocietypublishing.org" in u:
        ps.append(url.replace("/doi/full/","/doi/pdf/"))
        ps.append(url.replace("/doi/abs/","/doi/pdf/"))
    if "frontiersin.org" in u or "mdpi.com" in u:
        ps.append(url.rstrip("/")+"/pdf")
    return ps

def _direct(doi, dest):
    try:
        r = S.get(f"https://doi.org/{doi}", timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200: return False
        final = r.url
        if "linkinghub.elsevier.com" in final:
            x = re.search(r'href=["\']([^"\']*sciencedirect[^"\']*)["\']', r.text)
            if x: r = S.get(x.group(1), timeout=TIMEOUT, allow_redirects=True); final = r.url
        for pat in [r'<meta[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']',
                    r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']']:
            x = re.search(pat, r.text, re.I)
            if x and _dl(x.group(1), dest): return True
        for u in _pub_patterns(final, doi):
            if _dl(u, dest): return True
        return False
    except: return False

def download_pdf(doi, dl_dir):
    safe = re.sub(r'[^\w\-.]','_',doi)+".pdf"
    dest = dl_dir / safe
    for name, fn in [("unpaywall",_unpaywall),("crossref",_crossref_links),("europepmc",_europepmc),("scihub",_scihub),("direct",_direct)]:
        if fn(doi, dest): return (True, dest, name)
        time.sleep(0.5)
    if dest.exists(): dest.unlink()
    return (False, None, "")

# ── Zotero SQLite ────────────────────────────────────────────
class ZDB:
    ATT=3; LM=0; LIB=1
    TYPES={"journalArticle":22,"book":2,"bookSection":5,"conferencePaper":27,
           "thesis":7,"report":13,"preprint":39,"webpage":33}
    def __init__(self, zdir):
        self.db = Path(zdir)/"zotero.sqlite"; self.stor = Path(zdir)/"storage"
        self.c = None; self._k = set(); self._f = {}; self._ct = {}
    def connect(self):
        self.c = sqlite3.connect(str(self.db)); self.c.execute("PRAGMA journal_mode=WAL")
        c = self.c.cursor()
        c.execute("SELECT key FROM items"); self._k={r[0] for r in c.fetchall()}
        c.execute("SELECT fieldName,fieldID FROM fields"); self._f={r[0]:r[1] for r in c.fetchall()}
        c.execute("SELECT creatorType,creatorTypeID FROM creatorTypes"); self._ct={r[0]:r[1] for r in c.fetchall()}
    def close(self):
        if self.c: self.c.close(); self.c=None
    def backup(self):
        bp = self.db.parent/f"zotero_backup_{int(time.time())}.sqlite"
        shutil.copy2(self.db, bp); return bp
    def _key(self):
        ch = "23456789ABCDEFGHIJKMNPQRSTUVWXZ"  # Zotero valid key chars (no 0,1,L,O)
        while True:
            k="".join(random.choices(ch,k=8))
            if k not in self._k: self._k.add(k); return k
    def _now(self): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    def _val(self, cur, v):
        cur.execute("SELECT valueID FROM itemDataValues WHERE value=?",(v,))
        r=cur.fetchone()
        if r: return r[0]
        cur.execute("INSERT INTO itemDataValues (value) VALUES (?)",(v,)); return cur.lastrowid

    def find_coll(self, name):
        c=self.c.cursor(); c.execute("SELECT collectionID FROM collections WHERE collectionName=?",(name,))
        r=c.fetchone(); return r[0] if r else None
    def make_coll(self, name):
        c=self.c.cursor()
        c.execute("INSERT INTO collections (collectionName,libraryID,key,clientDateModified,version,synced) VALUES (?,?,?,?,0,0)",
                  (name,self.LIB,self._key(),self._now())); self.c.commit(); return c.lastrowid
    def has_doi(self, doi):
        fid=self._f.get("DOI")
        if not fid: return None
        c=self.c.cursor()
        c.execute("SELECT i.key FROM items i JOIN itemData d ON i.itemID=d.itemID JOIN itemDataValues v ON d.valueID=v.valueID WHERE d.fieldID=? AND v.value=?",(fid,doi))
        r=c.fetchone(); return r[0] if r else None
    def add_item(self, m, cid=None):
        c=self.c.cursor(); k=self._key(); ts=self._now(); tid=self.TYPES.get(m.item_type,22)
        c.execute("INSERT INTO items (itemTypeID,libraryID,key,dateAdded,dateModified,clientDateModified,version,synced) VALUES (?,?,?,?,?,?,0,0)",(tid,self.LIB,k,ts,ts,ts))
        iid=c.lastrowid
        for fn,fv in {"title":m.title,"abstractNote":m.abstract,"date":m.date,"DOI":m.doi,"url":m.url,"volume":m.volume,"issue":m.issue,"pages":m.pages,"publicationTitle":m.journal}.items():
            if not fv: continue
            fid=self._f.get(fn)
            if not fid: continue
            c.execute("INSERT OR IGNORE INTO itemData VALUES (?,?,?)",(iid,fid,self._val(c,fv)))
        for idx,a in enumerate(m.authors):
            ct=self._ct.get(a.get("creatorType","author"),1)
            fn,ln=a.get("firstName",""),a.get("lastName","")
            c.execute("SELECT creatorID FROM creators WHERE firstName=? AND lastName=?",(fn,ln))
            r=c.fetchone()
            if r: crid=r[0]
            else: c.execute("INSERT INTO creators (firstName,lastName) VALUES (?,?)",(fn,ln)); crid=c.lastrowid
            c.execute("INSERT INTO itemCreators VALUES (?,?,?,?)",(iid,crid,ct,idx))
        if cid: c.execute("INSERT OR IGNORE INTO collectionItems VALUES (?,?,0)",(cid,iid))
        self.c.commit(); return k

    def add_pdf(self, pk, pdf):
        c=self.c.cursor()
        c.execute("SELECT itemID FROM items WHERE key=?",(pk,)); r=c.fetchone()
        if not r: return pk
        pid=r[0]
        c.execute("SELECT 1 FROM items i JOIN itemAttachments a ON i.itemID=a.itemID WHERE a.parentItemID=? AND a.contentType='application/pdf'",(pid,))
        if c.fetchone(): return pk
        ak=self._key(); ts=self._now()
        ad=self.stor/ak; ad.mkdir(parents=True,exist_ok=True)
        fn=pdf.name; shutil.copy2(pdf,ad/fn)
        md5=hashlib.md5(open(pdf,"rb").read()).hexdigest()
        mt=int(pdf.stat().st_mtime*1000)
        c.execute("INSERT INTO items (itemTypeID,libraryID,key,dateAdded,dateModified,clientDateModified,version,synced) VALUES (?,?,?,?,?,?,0,0)",(self.ATT,self.LIB,ak,ts,ts,ts))
        aid=c.lastrowid
        c.execute("INSERT INTO itemAttachments (itemID,parentItemID,linkMode,contentType,path,syncState,storageModTime,storageHash) VALUES (?,?,?,?,?,0,?,?)",(aid,pid,self.LM,'application/pdf',f"storage:{fn}",mt,md5))
        tfid=self._f.get("title")
        if tfid:
            c.execute("INSERT OR IGNORE INTO itemData VALUES (?,?,?)",(aid,tfid,self._val(c,fn)))
        self.c.commit(); return ak

# ── 파이프라인 (백그라운드 스레드) ───────────────────────────
def run_pipeline(dois, zdir, coll, skip_existing):
    global PROGRESS
    LOG.clear()
    PROGRESS = {"current":0,"total":len(dois),"running":True,"done":False,"ok":0,"fail":0,"skip":0}
    dl_dir = Path("/tmp/doi2zotero_pdfs"); dl_dir.mkdir(exist_ok=True)
    try:
        zdb = ZDB(zdir); zdb.connect()
        bp = zdb.backup(); _log(f"DB 백업: {bp.name}")
        cid = None
        if coll:
            cid = zdb.find_coll(coll)
            if cid: _log(f"컬렉션 '{coll}' 사용")
            else: cid = zdb.make_coll(coll); _log(f"컬렉션 '{coll}' 생성")
        for i, doi in enumerate(dois):
            if not PROGRESS["running"]: _log("중지됨","skip"); break
            _log(f"[{i+1}/{len(dois)}] {doi}")
            PROGRESS["current"] = i+1
            if skip_existing:
                ex = zdb.has_doi(doi)
                if ex: _log(f"  이미 존재 ({ex})","skip"); PROGRESS["skip"]+=1; continue
            meta = fetch_meta(doi)
            _log(f"  {meta.title[:60]}")
            ok, pdf_path, src = download_pdf(doi, dl_dir)
            key = zdb.add_item(meta, cid)
            if ok:
                zdb.add_pdf(key, pdf_path)
                _log(f"  ✓ 완료 [{src}] → {key}","ok"); PROGRESS["ok"]+=1
            else:
                _log(f"  ✗ PDF 없음 (item {key})","fail"); PROGRESS["fail"]+=1
            time.sleep(DELAY)
        zdb.close()
    except Exception as e:
        _log(f"오류: {e}","fail")
    PROGRESS["running"]=False; PROGRESS["done"]=True
    _log("완료! Zotero를 실행하면 반영됩니다.")

# ── HTML 프론트엔드 ──────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DOI-to-Zotero</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0f172a;color:#e2e8f0;min-height:100vh;padding:24px}
.container{max-width:760px;margin:0 auto}
h1{font-size:1.8rem;color:#38bdf8;margin-bottom:4px}
.sub{color:#94a3b8;margin-bottom:20px;font-size:.9rem}
.card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:16px;
  border:1px solid #334155}
label{display:block;font-weight:600;color:#cbd5e1;margin-bottom:6px;font-size:.9rem}
input[type=text]{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #475569;
  background:#0f172a;color:#e2e8f0;font-size:.95rem;outline:none}
input[type=text]:focus{border-color:#38bdf8}
textarea{width:100%;padding:12px;border-radius:8px;border:1px solid #475569;
  background:#0f172a;color:#e2e8f0;font-family:'Courier New',monospace;
  font-size:.9rem;resize:vertical;min-height:140px;outline:none}
textarea:focus{border-color:#38bdf8}
.row{display:flex;gap:8px;align-items:center}
.row input{flex:1}
.btn{padding:10px 20px;border-radius:8px;border:none;font-weight:700;
  font-size:.95rem;cursor:pointer;transition:all .15s}
.btn-primary{background:#2563eb;color:#fff}.btn-primary:hover{background:#3b82f6}
.btn-primary:disabled{background:#334155;color:#64748b;cursor:not-allowed}
.btn-danger{background:#dc2626;color:#fff}.btn-danger:hover{background:#ef4444}
.btn-secondary{background:#334155;color:#cbd5e1}.btn-secondary:hover{background:#475569}
.btn-sm{padding:8px 14px;font-size:.85rem}
.progress-wrap{background:#1e293b;border-radius:99px;height:28px;overflow:hidden;
  border:1px solid #334155;position:relative}
.progress-bar{height:100%;background:linear-gradient(90deg,#2563eb,#38bdf8);
  transition:width .3s;border-radius:99px}
.progress-label{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:.85rem;font-weight:600;color:#fff}
</style>
</head><body>
<div class="container">
<h1>DOI-to-Zotero Auto-Crawler</h1>
<p class="sub">DOI 리스트 → 메타데이터 → PDF 다운로드 → Zotero 자동 등록</p>
"""

HTML += r"""
<div class="card">
  <label>Zotero 데이터 경로</label>
  <div class="row">
    <input type="text" id="zpath" value="ZOTERO_DEFAULT" placeholder="~/Zotero">
  </div>
  <div style="height:10px"></div>
  <label>컬렉션 이름 <span style="color:#64748b;font-weight:400">(선택사항 — 비워두면 미분류)</span></label>
  <input type="text" id="coll" placeholder="예: Normalization Papers">
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <label style="margin:0">DOI 목록</label>
    <label style="margin:0;cursor:pointer" class="btn btn-secondary btn-sm"
      onclick="document.getElementById('fup').click()">파일 불러오기
      <input type="file" id="fup" accept=".txt,.csv" style="display:none"
        onchange="loadFile(this)">
    </label>
  </div>
  <textarea id="dois" placeholder="10.1016/j.neuron.2020.04.002&#10;10.1038/s41586-021-03506-2&#10;..."></textarea>
  <div style="margin-top:6px;color:#38bdf8;font-size:.85rem" id="count">파싱된 DOI: 0</div>
</div>

<div class="card" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
  <button class="btn btn-primary" id="runBtn" onclick="startRun()">▶  실행</button>
  <button class="btn btn-danger" id="stopBtn" onclick="stopRun()" disabled>■  중지</button>
  <label style="margin:0;display:flex;align-items:center;gap:6px;cursor:pointer;font-weight:400">
    <input type="checkbox" id="skipChk" checked> 이미 있는 DOI 스킵
  </label>
</div>

<div class="card">
  <div class="progress-wrap">
    <div class="progress-bar" id="pbar" style="width:0%"></div>
    <div class="progress-label" id="plabel">대기 중</div>
  </div>
</div>
"""

HTML += r"""
<div class="card" style="padding:0;overflow:hidden">
  <div style="padding:10px 16px;background:#0f172a;border-bottom:1px solid #334155;
    font-size:.85rem;color:#94a3b8;font-weight:600">로그</div>
  <div id="logbox" style="height:260px;overflow-y:auto;padding:8px 14px;
    font-family:'Courier New',monospace;font-size:.82rem;line-height:1.6;
    background:#0a0e1a"></div>
</div>

<div class="card" style="text-align:center">
  <div id="result" style="font-size:1.1rem;font-weight:700;color:#94a3b8">결과가 여기에 표시됩니다</div>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const doiRe = /10\.\d{4,9}\/[^\s,;"'\]}>]+/g;

// DOI 카운트 실시간 업데이트
$('dois').addEventListener('input', () => {
  const m = ($('dois').value.match(doiRe) || []);
  const unique = [...new Set(m.map(d => d.replace(/\.$/,'')))];
  $('count').textContent = `파싱된 DOI: ${unique.length}`;
});

// 파일 불러오기
function loadFile(inp) {
  const f = inp.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = e => { $('dois').value = e.target.result; $('dois').dispatchEvent(new Event('input')); };
  r.readAsText(f);
}

// 로그 색상 맵
const tagColor = {ok:'#4ec9b0', fail:'#f44747', skip:'#dcdcaa', info:'#569cd6'};
let pollId = null, logIdx = 0;

function startRun() {
  const dois = $('dois').value.trim();
  const zpath = $('zpath').value.trim();
  const coll = $('coll').value.trim();
  const skip = $('skipChk').checked;
  if (!dois) { alert('DOI를 입력하세요'); return; }
  if (!zpath) { alert('Zotero 경로를 입력하세요'); return; }

  $('logbox').innerHTML = '';
  $('result').textContent = '';
  $('result').style.color = '#94a3b8';
  logIdx = 0;

  fetch('/api/start', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({dois, zpath, coll, skip})
  }).then(r => r.json()).then(d => {
    if (d.error) { alert(d.error); return; }
    $('runBtn').disabled = true;
    $('stopBtn').disabled = false;
    pollId = setInterval(pollStatus, 800);
  });
}

function stopRun() {
  fetch('/api/stop', {method:'POST'});
}
</script>
"""

HTML += r"""
<script>
function pollStatus() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    // 진행 바
    const pct = d.total > 0 ? Math.round(d.current / d.total * 100) : 0;
    $('pbar').style.width = pct + '%';
    $('plabel').textContent = d.running ? `${d.current} / ${d.total} (${pct}%)` :
      (d.done ? '완료' : '대기 중');

    // 로그 업데이트
    fetch('/api/logs?from=' + logIdx).then(r=>r.json()).then(logs => {
      logs.forEach(l => {
        const div = document.createElement('div');
        div.style.color = tagColor[l.tag] || '#569cd6';
        div.textContent = l.ts + '  ' + l.msg;
        $('logbox').appendChild(div);
      });
      logIdx += logs.length;
      if (logs.length > 0) $('logbox').scrollTop = $('logbox').scrollHeight;
    });

    // 결과
    if (d.done) {
      clearInterval(pollId);
      $('runBtn').disabled = false;
      $('stopBtn').disabled = true;
      const total = d.ok + d.fail + d.skip;
      $('result').innerHTML =
        `<span style="color:#4ec9b0">성공 ${d.ok}</span> / ` +
        `<span style="color:#f44747">실패 ${d.fail}</span> / ` +
        `<span style="color:#dcdcaa">스킵 ${d.skip}</span>` +
        `<span style="color:#94a3b8"> (총 ${total})</span>`;
    }
  });
}
</script></body></html>"""

# Zotero 기본 경로를 HTML에 삽입
HTML = HTML.replace("ZOTERO_DEFAULT", DEFAULT_ZOTERO)

# ── HTTP 서버 ────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass   # 콘솔 로그 억제

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path.startswith("/api/status"):
            self._json(PROGRESS)
        elif self.path.startswith("/api/logs"):
            # ?from=N  → N번째 이후 로그만 반환
            fr = 0
            if "from=" in self.path:
                try: fr = int(self.path.split("from=")[1])
                except: pass
            self._json(LOG[fr:])
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/start":
            if PROGRESS.get("running"):
                self._json({"error": "이미 실행 중입니다"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
            dois_text = body.get("dois","")
            zpath = body.get("zpath", DEFAULT_ZOTERO)
            coll = body.get("coll","") or None
            skip = body.get("skip", True)

            dois = parse_dois(dois_text)
            if not dois:
                self._json({"error":"DOI를 찾을 수 없습니다"}); return
            zdb_path = Path(zpath)/"zotero.sqlite"
            if not zdb_path.exists():
                self._json({"error":f"Zotero DB 없음: {zdb_path}"}); return
            # Zotero 실행 체크
            try:
                r = subprocess.run(["pgrep","-xi","zotero"], capture_output=True)
                if r.returncode == 0:
                    self._json({"error":"Zotero가 실행 중! 종료 후 다시 시도하세요."}); return
            except: pass

            t = threading.Thread(target=run_pipeline, args=(dois,zpath,coll,skip), daemon=True)
            t.start()
            self._json({"ok":True,"count":len(dois)})

        elif self.path == "/api/stop":
            PROGRESS["running"] = False
            self._json({"ok":True})
        else:
            self.send_error(404)


# ── 메인 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  DOI-to-Zotero Auto-Crawler v2.0")
    print(f"  ================================")
    print(f"  서버 시작: http://localhost:{PORT}")
    print(f"  브라우저가 자동으로 열립니다.\n")
    print(f"  종료: Ctrl+C\n")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    # 1초 뒤 브라우저 자동 오픈
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료.")
        server.shutdown()

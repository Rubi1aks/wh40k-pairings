import os
import json
import urllib.request
import urllib.error
import openpyxl
import tempfile
import re
import time
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import minimax

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/viewer.html")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

class SheetRequest(BaseModel):
    url: str

class GenerateRequest(BaseModel):
    url: str
    sheet_name: str
    team_size: int
    format_type: str
    scale: int

class SavePayload(BaseModel):
    data: str

def _extract_gsheet_id(url):
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    if not m: raise ValueError("Неверная ссылка")
    return m.group(1)

def download_gsheet(url):
    gid = _extract_gsheet_id(url)
    export_url = f'https://docs.google.com/spreadsheets/d/{gid}/export?format=xlsx'
    req = urllib.request.Request(export_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if not data.startswith(b'PK'): raise ValueError("Google вернул не xlsx. Проверьте доступ.")
    fd, path = tempfile.mkstemp(suffix='.xlsx')
    with os.fdopen(fd, 'wb') as f: f.write(data)
    return path

@app.post("/api/sheets")
def get_sheets(req: SheetRequest):
    try:
        path = download_gsheet(req.url)
        wb = openpyxl.load_workbook(path, read_only=True)
        os.remove(path)
        return {"sheets": wb.sheetnames}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/generate")
def generate(req: GenerateRequest):
    try:
        path = download_gsheet(req.url)
        wb = openpyxl.load_workbook(path, data_only=True)
        os.remove(path)
        
        if req.sheet_name not in wb.sheetnames:
            raise ValueError("Лист не найден")
        
        ws = wb[req.sheet_name]
        players, enemies = minimax.validate_table(ws)
        our = [p[0] for p in players]
        our_row = {p[0]: p[1] for p in players}
        enemy_names = [e[0] for e in enemies]
        cM = {e[0]: e[1] for e in enemies}
        cN = {e[0]: e[2] for e in enemies}
        
        sm = {p: {} for p in our}; sn = {p: {} for p in our}
        for p in our:
            for e in enemy_names:
                mv = ws.cell(row=our_row[p], column=cM[e]).value
                nv = ws.cell(row=our_row[p], column=cN[e]).value
                sm[p][e] = minimax._to_score(mv)
                sn[p][e] = minimax._to_score(nv)

        fmt = req.format_type
        if req.team_size in (3, 5): fmt = 'champ_champ'
            
        report = minimax.generate_pairing(our, enemy_names, sm, sn, req.scale, fmt)
        
        # Винрейты тянем из БД, чтобы прикрепить к отчёту
        try:
            url = f"{SUPABASE_URL}/winrates_cache?id=eq.default_winrates&select=data"
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            r = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(r) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
                if rows and len(rows) > 0:
                    report['winrates'] = json.loads(rows[0]['data'])
        except Exception as e:
            print(f"Не удалось прикрепить винрейты: {e}")

        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/db/save")
def db_save(req: SavePayload):
    url = f"{SUPABASE_URL}/shared_room"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
    payload = json.dumps([{"id": "default_team_room", "data": req.data}]).encode("utf-8")
    try:
        r = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        urllib.request.urlopen(r)
        return {"status": "ok"}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Supabase Error: {e.read().decode('utf-8')}")

@app.get("/api/db/load")
def db_load():
    url = f"{SUPABASE_URL}/shared_room?id=eq.default_team_room&select=data"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(r) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
            if rows and len(rows) > 0:
                return Response(content=rows[0]['data'], media_type="application/json")
            return Response(content="{}", media_type="application/json")
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Supabase Error: {e.read().decode('utf-8')}")

@app.get("/api/db/winrates")
def db_get_winrates():
    url = f"{SUPABASE_URL}/winrates_cache?id=eq.default_winrates&select=data"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(r) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
            if rows and len(rows) > 0:
                return Response(content=rows[0]['data'], media_type="application/json")
            return Response(content="{}", media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/db/winrates/recalc")
def recalc_winrates():
    try:
        matrix = minimax.scrape_winrates(log=print)
        payload = {
            'source': minimax.LH_BASE,
            'scraped_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'include_rtt': minimax.WINRATES_INCLUDE_RTTS,
            'factions': [name for _, name in minimax.WINRATES_FACTIONS],
            'matrix': matrix,
        }
        json_str = json.dumps(payload, ensure_ascii=False)
        
        url = f"{SUPABASE_URL}/winrates_cache"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"
        }
        db_payload = json.dumps([{"id": "default_winrates", "data": json_str}]).encode("utf-8")
        r = urllib.request.Request(url, data=db_payload, headers=headers, method="POST")
        urllib.request.urlopen(r)
        
        return {"status": "ok", "matrix": matrix}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

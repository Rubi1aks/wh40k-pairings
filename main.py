import os
import json
import urllib.request
import openpyxl
import io
import tempfile
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import minimax
import re

app = FastAPI()

# Раздаём статику (html, css, js) из папки static
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def read_root():
    return FileResponse("static/index.html")


class SheetRequest(BaseModel):
    url: str


class GenerateRequest(BaseModel):
    url: str
    sheet_name: str
    team_size: int
    format_type: str  # 'champ_ref' or 'champ_champ'
    scale: int  # 5, 7, 10


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
    if not data.startswith(b'PK'): raise ValueError("Google вернул не xlsx")
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

        # Загружаем таблицу (упрощенно, используем валидатор)
        players, enemies = minimax.validate_table(ws)
        our = [p[0] for p in players]
        our_row = {p[0]: p[1] for p in players}
        enemy_names = [e[0] for e in enemies]
        cM = {e[0]: e[1] for e in enemies}
        cN = {e[0]: e[2] for e in enemies}

        sm = {p: {} for p in our};
        sn = {p: {} for p in our}
        for p in our:
            for e in enemy_names:
                mv = ws.cell(row=our_row[p], column=cM[e]).value
                nv = ws.cell(row=our_row[p], column=cN[e]).value
                sm[p][e] = minimax._to_score(mv)
                sn[p][e] = minimax._to_score(nv)

        # Определяем дефендеров на основе N
        def_count = 2
        if req.team_size == 3:
            def_count = 1
        elif req.team_size in (5, 6):
            def_count = 2
        elif req.team_size == 8:
            def_count = 3

        # Генерируем
        report = minimax.generate_pairing(our, enemy_names, sm, sn, req.scale, req.format_type, def_count)

        # Добавляем инфу о винрейтах
        try:
            with open('winrates.json', 'r', encoding='utf-8') as f:
                report['winrates'] = json.load(f)
        except:
            pass

        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
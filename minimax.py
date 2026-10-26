import json
import re
import time
import urllib.request
from collections import namedtuple
from itertools import combinations

THRESHOLD = 56.0
MIN_SCORE = 0.0
MAX_SCORE = 10.0

class ValidationError(Exception):
    pass

def validate_table(ws):
    players = []
    r = 3
    while r <= ws.max_row:
        v = ws.cell(row=r, column=1).value
        if v is None:
            r += 1
            continue
        s = str(v).strip()
        if s.startswith(("Оценки", "Система", "Столы")):
            break
        players.append((s, r))
        r += 1

    if not players:
        raise ValidationError("Не найдены игроки в столбце A.")

    enemies = []
    for c in range(2, ws.max_column + 1):
        lab = ws.cell(row=2, column=c).value
        if lab is None: continue
        ls = str(lab).strip().lower()
        if "не мой" in ls: continue
        if "мой" in ls:
            nxt = ws.cell(row=2, column=c + 1).value
            ns = str(nxt).strip().lower() if nxt is not None else ""
            if "не мой" not in ns: continue
            en = ws.cell(row=1, column=c).value or ws.cell(row=1, column=c + 1).value
            en = str(en or f"Enemy{len(enemies) + 1}").strip()
            enemies.append((en, c, c + 1))

    if not enemies:
        raise ValidationError("Не найдены столбцы врагов («Мой / Не мой»).")

    return players, enemies

def _to_score(v):
    if v is None: return 0.0
    if isinstance(v, str) and not v.strip(): return 0.0
    try: return float(v)
    except: return 0.0

def map_score(score, scale):
    if scale == 10: return score * 2.0
    elif scale == 5: return score * 4.0
    elif scale == 7:
        table = {0:0, 1:3, 2:5, 3:7, 4:10, 5:13, 6:17, 7:20}
        return float(table.get(int(round(score)), 0))
    return score * 2.0

State = namedtuple('State', [
    'our_hand', 'enemy_hand', 'cycle', 'phase',
    'D_our', 'D_enemy', 'our_offers', 'enemy_offers', 'X', 'U',
])

OUR_TURN = {'def_our', 'offers_our', 'pick_our'}

class Analyzer:
    def __init__(self, our, enemies, sm, sn, scale=10, format_type='champ_ref'):
        self.our = list(our)
        self.enemies = list(enemies)
        self.format_type = format_type
        
        self.MY = {}
        self.NOT = {}
        for p in our:
            for e in enemies:
                self.MY[(p, e)] = map_score(sm[p][e], scale)
                self.NOT[(p, e)] = map_score(sn[p][e], scale)
                
        self._vcache = {}
        self._ncache = {}
        self.nodes = []

    def s_my(self, p, e): return self.MY[(p, e)]
    def s_not(self, p, e): return self.NOT[(p, e)]

    def _options(self, st):
        ph = st.phase
        if ph == 'def_our':
            return [(d, [], 0.0, st._replace(D_our=d, phase='def_enemy')) for d in sorted(st.our_hand)]
        if ph == 'def_enemy':
            return [(e, [], 0.0, st._replace(D_enemy=e, phase='offers_our')) for e in sorted(st.enemy_hand)]
        if ph == 'offers_our':
            rest = sorted(st.our_hand - {st.D_our})
            k = min(2, len(rest))
            return [(off, [], 0.0, st._replace(our_offers=frozenset(off), phase='offers_enemy')) for off in combinations(rest, k)]
        if ph == 'offers_enemy':
            rest = sorted(st.enemy_hand - {st.D_enemy})
            k = min(2, len(rest))
            return [(off, [], 0.0, st._replace(enemy_offers=frozenset(off), phase='pick_our')) for off in combinations(rest, k)]
        if ph == 'pick_our':
            out = []
            for x in sorted(st.enemy_offers):
                s = self.s_my(st.D_our, x)
                out.append((x, [[st.D_our, x, 0, s]], s, st._replace(X=x, phase='pick_enemy')))
            return out
        if ph == 'pick_enemy':
            out = []
            for u in sorted(st.our_offers):
                s = self.s_not(u, st.D_enemy)
                pairs = [[u, st.D_enemy, 1, s]]
                delta = s
                
                nh = frozenset(st.our_hand - {st.D_our, u})
                ne = frozenset(st.enemy_hand - {st.D_enemy, st.X})
                
                if len(nh) >= 2:
                    child = State(nh, ne, st.cycle + 1, 'def_our', None, None, None, None, None, None)
                else:
                    our_ref = next(iter(st.our_offers - {u}))
                    en_ref = next(iter(st.enemy_offers - {st.X}))
                    
                    our_unt_set = st.our_hand - {st.D_our} - st.our_offers
                    en_unt_set = st.enemy_hand - {st.D_enemy} - st.enemy_offers
                    
                    if not our_unt_set or not en_unt_set:
                        s_rr = self.s_not(our_ref, en_ref)
                        delta += s_rr
                        pairs.append([our_ref, en_ref, 2, s_rr])
                    else:
                        our_unt = next(iter(our_unt_set))
                        en_unt = next(iter(en_unt_set))
                        
                        if self.format_type == 'champ_champ':
                            sc = self.s_my(our_unt, en_unt)
                            sr = self.s_not(our_ref, en_ref)
                            pairs.append([our_unt, en_unt, 2, sc])
                            pairs.append([our_ref, en_ref, 3, sr])
                        else:
                            sc = self.s_my(our_unt, en_ref)
                            sr = self.s_not(our_ref, en_unt)
                            pairs.append([our_unt, en_ref, 2, sc])
                            pairs.append([our_ref, en_unt, 3, sr])
                            
                        delta += sc + sr
                    
                    child = None
                
                out.append((u, pairs, delta, child))
            return out
        return []

    def value(self, st):
        if st in self._vcache: return self._vcache[st]
        opts = self._options(st)
        is_our = st.phase in OUR_TURN
        best = None
        for _, _, delta, child in opts:
            if child is None: v = delta
            else:
                cv = self.value(child)
                if cv is None: continue
                v = cv + delta
            if best is None or (is_our and v > best) or (not is_our and v < best):
                best = v
        self._vcache[st] = best
        return best

    def _label(self, st):
        c, ph = st.cycle, st.phase
        return {
            'def_our': f'Наш дефендер (цикл {c})',
            'def_enemy': f'Их дефендер (цикл {c})',
            'offers_our': f'Наши атакующие против {st.D_enemy} (цикл {c})',
            'offers_enemy': f'Их атакующие против {st.D_our} (цикл {c})',
            'pick_our': f'{st.D_our} выбирает оппонента (цикл {c})',
            'pick_enemy': f'{st.D_enemy} выбирает оппонента (цикл {c})',
        }.get(ph, ph)

    @staticmethod
    def _astr(a):
        return ' + '.join(sorted(a)) if isinstance(a, tuple) else str(a)

    def get_id(self, st):
        if st in self._ncache: return self._ncache[st]
        nid = len(self.nodes); self._ncache[st] = nid
        self.nodes.append(None)
        self.nodes[nid] = self._build(st, nid)
        return nid

    def _build(self, st, nid):
        opts = self._options(st)
        is_our = st.phase in OUR_TURN
        raw_opts = []
        for action, pairs, delta, child in opts:
            child_id = -1 if child is None else self.get_id(child)
            raw_opts.append({'a': self._astr(action), 'd': float(delta), 'c': child_id, 'p': pairs})
            
        scored = []
        for i, o in enumerate(raw_opts):
            if o['c'] == -1:
                v = o['d']
                stats = {'min': v, 'max': v, 'avg': v, 'std': 0.0, 'hist': {int(round(v)): 1}, 'count': 1}
            else:
                child_node = self.nodes[o['c']]
                if child_node is None or child_node['v'] is None: continue
                base = o['d']
                cs = child_node['s']
                v = base + child_node['v']
                hist = {int(round(k + base)): c for k, c in cs['hist'].items()}
                stats = {'min': cs['min'] + base, 'max': cs['max'] + base, 'avg': cs['avg'] + base,
                         'std': cs['std'], 'hist': hist, 'count': cs['count']}
            scored.append((i, v, stats))

        if not scored:
            return {'c': st.cycle, 'k': 0 if is_our else 1, 'ph': st.phase,
                    'l': self._label(st), 'v': None, 's': None, 'o': raw_opts, 'ci': None}

        if is_our: chosen = max(scored, key=lambda x: (x[1], x[2]['avg']))
        else: chosen = min(scored, key=lambda x: (x[1], x[2]['avg']))
        best_v = chosen[1]
        chosen_idx = chosen[0]
        for i, v, _ in scored:
            raw_opts[i]['op'] = 1 if abs(v - best_v) < 1e-6 else 0

        if is_our:
            node_stats = chosen[2]
        else:
            hist = {}; count = 0; total = 0.0; mn = None; mx = None
            for _, _, s in scored:
                for k, c in s['hist'].items():
                    hist[k] = hist.get(k, 0) + c; count += c; total += k * c
                    if mn is None or k < mn: mn = k
                    if mx is None or k > mx: mx = k
            avg = total / count if count else 0
            sq = sum((k - avg)**2 * c for k, c in hist.items())
            std = (sq / count)**0.5 if count else 0
            node_stats = {'min': mn, 'max': mx, 'avg': avg, 'std': std, 'hist': hist, 'count': count}

        return {
            'c': st.cycle, 'k': 0 if is_our else 1, 'ph': st.phase,
            'l': self._label(st), 'v': round(best_v, 2), 'ci': chosen_idx,
            's': {'min': node_stats['min'], 'max': node_stats['max'],
                  'avg': round(node_stats['avg'], 4), 'std': round(node_stats['std'], 4),
                  'hist': node_stats['hist'], 'count': node_stats['count']},
            'o': raw_opts,
        }

    def build(self):
        init = State(frozenset(self.our), frozenset(self.enemies), 1, 'def_our', None, None, None, None, None, None)
        return self.get_id(init)

def generate_pairing(players, enemies, sm, sn, scale, format_type):
    an = Analyzer(players, enemies, sm, sn, scale, format_type)
    root = an.build()
    return {
        'players': players, 'enemies': enemies,
        'threshold': THRESHOLD, 'root_id': root, 'nodes': an.nodes,
        'matrix': {'my': [[sm[p][e] for e in enemies] for p in players],
                   'not': [[sn[p][e] for e in enemies] for p in players]}
    }

# ============================================================ winrates

LH_BASE = "https://listhammer.info"
WINRATES_INCLUDE_RTTS = True

WINRATES_FACTIONS = [
    ("adepta-sororitas", "Adepta Sororitas"),
    ("adeptus-custodes", "Adeptus Custodes"),
    ("adeptus-mechanicus", "Adeptus Mechanicus"),
    ("aeldari", "Aeldari"),
    ("astra-militarum", "Astra Militarum"),
    ("black-templars", "Black Templars"),
    ("blood-angels", "Blood Angels"),
    ("chaos-daemons", "Chaos Daemons"),
    ("chaos-knights", "Chaos Knights"),
    ("chaos-space-marines", "Chaos Space Marines"),
    ("dark-angels", "Dark Angels"),
    ("death-guard", "Death Guard"),
    ("deathwatch", "Deathwatch"),
    ("drukhari", "Drukhari"),
    ("emperors-children", "Emperor's Children"),
    ("genestealer-cult", "Genestealer Cult"),
    ("grey-knights", "Grey Knights"),
    ("imperial-agents", "Imperial Agents"),
    ("imperial-knights", "Imperial Knights"),
    ("leagues-of-votann", "Leagues of Votann"),
    ("necrons", "Necrons"),
    ("orks", "Orks"),
    ("space-marines", "Space Marines"),
    ("space-wolves", "Space Wolves"),
    ("tau-empire", "T'au Empire"),
    ("thousand-sons", "Thousand Sons"),
    ("tyranids", "Tyranids"),
    ("world-eaters", "World Eaters"),
]

NUXT_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
DEVAL_WRAPPERS = {'ShallowReactive', 'Reactive', 'Ref', 'ShallowRef',
                  'EmptyRef', 'EmptyShallowRef'}


def _devalue_decode(data):
    if not isinstance(data, list) or len(data) < 2:
        return None
    memo = {}

    def resolve(idx):
        if not isinstance(idx, int) or idx < 0 or idx >= len(data):
            return idx
        if idx in memo:
            return memo[idx]
        v = data[idx]
        if isinstance(v, dict):
            result = {}
            memo[idx] = result
            for k, sub in v.items():
                result[k] = resolve(sub)
            return result
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in DEVAL_WRAPPERS:
                inner = resolve(v[1])
                memo[idx] = inner
                return inner
            result = []
            memo[idx] = result
            for sub in v:
                result.append(resolve(sub))
            return result
        return v

    for i in range(len(data)):
        if isinstance(data[i], dict) and 'data' in data[i] and 'state' in data[i]:
            return resolve(i)
    return resolve(1) if len(data) > 1 else None


def _normalize_opponent_name(name):
    if not isinstance(name, str):
        return name
    if name.endswith(')') and ' (' in name:
        return name[:name.rindex(' (')]
    return name


def _fetch_winrates_for_faction(slug, log=print):
    params = []
    if WINRATES_INCLUDE_RTTS:
        params.append('includeRtt=true')
    qs = ('?' + '&'.join(params)) if params else ''
    url = f"{LH_BASE}/factions/{slug}{qs}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (personal wh40k pairing tool)',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode('utf-8', errors='replace')
    m = NUXT_RE.search(html)
    if not m:
        raise RuntimeError("__NUXT_DATA__ не найден в HTML")
    raw = json.loads(m.group(1))
    root = _devalue_decode(raw)
    if not isinstance(root, dict) or 'data' not in root:
        raise RuntimeError("не удалось раскодировать Nuxt-полезную нагрузку")
    page_data = root['data']
    if not isinstance(page_data, dict):
        raise RuntimeError("неожиданная структура root['data']")
    faction_payload = None
    for _, v in page_data.items():
        if isinstance(v, dict) and 'matchups' in v:
            faction_payload = v
            break
    if not faction_payload:
        raise RuntimeError("в payload нет matchups")
    matchups = {}
    for row in faction_payload.get('matchups', []):
        opp = row.get('opponentFaction')
        wr = row.get('winRate')
        if opp is None or wr is None:
            continue
        if isinstance(wr, str):
            try:
                wr = float(wr.rstrip('%').strip())
            except ValueError:
                continue
        matchups[_normalize_opponent_name(opp)] = float(wr)
    return matchups


def scrape_winrates(log=print):
    matrix = {}
    total = len(WINRATES_FACTIONS)
    for i, (slug, canonical) in enumerate(WINRATES_FACTIONS, 1):
        try:
            matchups = _fetch_winrates_for_faction(slug, log=log)
        except Exception as e:
            log(f"  ! [{i}/{total}] {slug}: {e}")
            time.sleep(1.5)
            continue
        matrix[canonical] = matchups
        log(f"  ✓ [{i}/{total}] {canonical}: {len(matchups)} matchups")
        time.sleep(1.0)
    return matrix

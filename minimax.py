import json
from collections import namedtuple
from itertools import combinations

THRESHOLD = 56.0


def map_score(score, scale):
    """Переводит оценку (0..scale) в очки (0..20)."""
    if scale == 10:
        return score * 2.0
    elif scale == 5:
        return score * 4.0
    elif scale == 7:
        table = {0: 0, 1: 3, 2: 5, 3: 7, 4: 10, 5: 13, 6: 17, 7: 20}
        return float(table.get(int(round(score)), 0))
    return score * 2.0


State = namedtuple('State', [
    'our_hand', 'enemy_hand', 'cycle', 'phase',
    'D_our', 'D_enemy', 'our_offers', 'enemy_offers', 'X', 'U',
])

OUR_TURN = {'def_our', 'offers_our', 'pick_our'}


class Analyzer:
    def __init__(self, our, enemies, sm, sn, scale=10, format_type='champ_ref', def_count=2):
        self.our = list(our)
        self.enemies = list(enemies)
        self.format_type = format_type
        self.def_count = def_count

        self.MY = {}
        self.NOT = {}
        for p in our:
            for e in enemies:
                self.MY[(p, e)] = map_score(sm[p][e], scale)
                self.NOT[(p, e)] = map_score(sn[p][e], scale)

        self._vcache = {}
        self._ncache = {}
        self.nodes = []

    def s_my(self, p, e):
        return self.MY[(p, e)]

    def s_not(self, p, e):
        return self.NOT[(p, e)]

    def _options(self, st):
        ph = st.phase
        if ph == 'def_our':
            return [(d, [], 0.0, st._replace(D_our=d, phase='def_enemy'))
                    for d in sorted(st.our_hand)]
        if ph == 'def_enemy':
            return [(e, [], 0.0, st._replace(D_enemy=e, phase='offers_our'))
                    for e in sorted(st.enemy_hand)]
        if ph == 'offers_our':
            rest = sorted(st.our_hand - {st.D_our})
            return [(off, [], 0.0, st._replace(our_offers=frozenset(off), phase='offers_enemy'))
                    for off in combinations(rest, 2)]
        if ph == 'offers_enemy':
            rest = sorted(st.enemy_hand - {st.D_enemy})
            return [(off, [], 0.0, st._replace(enemy_offers=frozenset(off), phase='pick_our'))
                    for off in combinations(rest, 2)]
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

                # Проверяем, закончился ли первый цикл (все дефендеры отработали)
                if len(st.our_hand) <= self.def_count:
                    # Финальный этап
                    if self.format_type == 'champ_champ':
                        # Чемпион с чемпионом, отказник с отказником
                        our_ref = next(iter(st.our_offers - {u}))
                        our_unt = next(iter(st.our_hand - {st.D_our} - st.our_offers))
                        en_ref = next(iter(st.enemy_offers - {st.X}))
                        en_unt = next(iter(st.enemy_hand - {st.D_enemy} - st.enemy_offers))
                        sc = self.s_my(our_unt, en_ref)  # Wait, champ vs champ?
                        # В champ_champ: unt vs unt, ref vs ref
                        sc = self.s_my(our_unt, en_unt)
                        sr = self.s_not(our_ref, en_ref)
                    else:
                        # champ_ref (как сейчас)
                        our_ref = next(iter(st.our_offers - {u}))
                        our_unt = next(iter(st.our_hand - {st.D_our} - st.our_offers))
                        en_ref = next(iter(st.enemy_offers - {st.X}))
                        en_unt = next(iter(st.enemy_hand - {st.D_enemy} - st.enemy_offers))
                        sc = self.s_my(our_unt, en_ref)
                        sr = self.s_not(our_ref, en_unt)

                    delta += sc + sr
                    pairs.append([our_unt, en_unt if self.format_type == 'champ_champ' else en_ref, 2, sc])
                    pairs.append([our_ref, en_ref if self.format_type == 'champ_champ' else en_unt, 3, sr])
                    child = None
                else:
                    nh = frozenset(st.our_hand - {st.D_our, u})
                    ne = frozenset(st.enemy_hand - {st.D_enemy, st.X})
                    child = State(nh, ne, st.cycle + 1, 'def_our', None, None, None, None, None, None)

                out.append((u, pairs, delta, child))
            return out
        return []

    def value(self, st):
        if st in self._vcache: return self._vcache[st]
        opts = self._options(st)
        is_our = st.phase in OUR_TURN
        best = None
        for _, _, delta, child in opts:
            if child is None:
                v = delta
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
        nid = len(self.nodes);
        self._ncache[st] = nid
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

        if is_our:
            chosen = max(scored, key=lambda x: (x[1], x[2]['avg']))
        else:
            chosen = min(scored, key=lambda x: (x[1], x[2]['avg']))
        best_v = chosen[1]
        chosen_idx = chosen[0]
        for i, v, _ in scored:
            raw_opts[i]['op'] = 1 if abs(v - best_v) < 1e-6 else 0

        if is_our:
            node_stats = chosen[2]
        else:
            hist = {};
            count = 0;
            total = 0.0;
            mn = None;
            mx = None
            for _, _, s in scored:
                for k, c in s['hist'].items():
                    hist[k] = hist.get(k, 0) + c;
                    count += c;
                    total += k * c
                    if mn is None or k < mn: mn = k
                    if mx is None or k > mx: mx = k
            avg = total / count if count else 0
            sq = sum((k - avg) ** 2 * c for k, c in hist.items())
            std = (sq / count) ** 0.5 if count else 0
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


def generate_pairing(players, enemies, sm, sn, scale, format_type, def_count):
    an = Analyzer(players, enemies, sm, sn, scale, format_type, def_count)
    root = an.build()
    return {
        'players': players, 'enemies': enemies,
        'threshold': THRESHOLD, 'root_id': root, 'nodes': an.nodes,
        'matrix': {'my': [[sm[p][e] for e in enemies] for p in players],
                   'not': [[sn[p][e] for e in enemies] for p in players]}
    }
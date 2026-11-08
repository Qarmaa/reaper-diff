#!/usr/bin/env python3
"""
rpp_diff.py — различия между двумя сессиями Reaper (.rpp) и сохранение различий в отдельную сессию .rpp.

    rpp_diff.py A.rpp B.rpp   (вместо .rpp можно указать папку проекта Reaper)  [--tol <сек>] [--json <файл>] [--report]
                [--out <файл.rpp | папка>] [--no-added] [--no-changed] [--no-removed] [--no-old]
                [--no-item-markers]

A — старая версия, B — новая. Сравнивается: дорожки (добавлены/удалены/изменены), item'ы (добавлены/удалены/
перемещены/обрезаны/фейды/громкость/mute/дорожка/источник/прочее), маркеры и регионы, параметры проекта.

Item'ы сопоставляются: по IGUID (если сессия — одна и та же, сохранённая в разных версиях), затем по содержимому
(файл + диапазон источника, затем «синхронизация» файла на таймлайне, затем перекрытие диапазонов) — так различия
находятся и между сессиями без общих GUID (например, после конвертации).

Сессия различий (--out) строится на B: сохраняются все дорожки B, но только item'ы-различия (добавленные — зелёные,
изменённые — жёлтые, с описанием в заметках item'а); дорожки без различий не переносятся (--keep-unchanged-tracks). Удалённые из A (и старые версии изменённых) кладутся на
отдельные дорожки «A ▸ имя» (красные / серые). Маркеры различий добавляются как маркеры/регионы.
Прогресс печатается строками `label: n/total`, итог — `Done: <путь>`.
"""
import argparse
import collections
import difflib
import json
import math
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Конвертация в Pro Tools (--pt) — движком установленной программы «Reaper to Pro Tools»: он идёт первым в пути,
# чтобы конвертер и сравнение работали с одним модулем rpp (сессия различий регистрируется в нём в памяти).
CONVERTER_DIRS = [os.path.expanduser('~/ReaperToProTools/ptx_tools'),
                  '/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools',
                  os.path.expanduser('~/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools')]
CONVERTER = next((d for d in CONVERTER_DIRS if os.path.isfile(os.path.join(d, 'reaper_to_ptx.py'))), None)
if CONVERTER:
    sys.path.insert(0, CONVERTER)
import rpp                                               # noqa: E402

DEFAULT_TOL = 1e-5            # секунд (≈ половина сэмпла при 48 кГц)

COLORS = {                    # 0xRRGGBB
    'added': 0x3FB950,
    'changed': 0xE3A008,
    'removed': 0xE5484D,
    'old': 0x7A8699,
}

# Ключи item'а, которые сравниваются отдельно или не несут смысла при сравнении «прочего».
_IGNORE_KEYS = {'IGUID', 'GUID', 'IID', 'SEL', 'YPOS', 'LASTSEL', 'RECPASS', 'POSITION', 'LENGTH', 'SOFFS',
                'FADEIN', 'FADEOUT', 'MUTE', 'VOLPAN', 'PLAYRATE', 'NAME', 'COLOR', 'LOCK', 'TAKECOLOR'}


# ---------------------------------------------------------------- разбор дерева с номерами строк

class Tree(rpp.Node):
    def __init__(self, name='', args=None):
        rpp.Node.__init__(self, name, args)
        self.start = 0
        self.end = 0
        self.eidx = []                 # номер строки для каждого элемента entries


def parse_tree(lines):
    root = Tree('REAPER_PROJECT')
    stack = [root]
    for i in range(1, len(lines)):
        text = lines[i].strip()
        if not text:
            continue
        cur = stack[-1]
        if text[0] == '>':
            cur.end = i
            stack.pop()
            if not stack:
                break
        elif text[0] == '<':
            tok = rpp.tokenize(text[1:])
            n = Tree(tok[0] if tok else '', tok[1:])
            n.start = i
            cur.entries.append(('child', n))
            cur.eidx.append(i)
            stack.append(n)
        else:
            tok = rpp.tokenize(text)
            if tok:
                cur.entries.append(('line', tok[0], tok[1:]))
                cur.eidx.append(i)
    while stack:                                   # файл оборван — закрываем
        stack.pop().end = len(lines) - 1
    return root


# ---------------------------------------------------------------- форматирование

def fmt_t(sec):
    sign = '-' if sec < 0 else ''
    ms = int(round(abs(sec) * 1000))
    h, r = divmod(ms, 3600000)
    m, r = divmod(r, 60000)
    return '%s%d:%02d:%02d.%03d' % (sign, h, m, r // 1000, r % 1000)


def fmt_dur(sec):
    return '%+.3f s' % sec


def db(v):
    return '-inf dB' if v <= 1e-9 else '%+.2f dB' % (20 * math.log10(v))


def q(s):
    if '"' not in s:
        return '"%s"' % s
    if "'" not in s:
        return "'%s'" % s
    return '`%s`' % s.replace('`', "'")


def native_color(rgb):
    return ((rgb >> 16) & 255) | (rgb & 0xFF00) | ((rgb & 255) << 16) | 0x1000000


def new_guid():
    return '{%s}' % str(uuid.uuid4()).upper()


def base(path):
    return path.replace('\\', '/').rsplit('/', 1)[-1].lower()


# ---------------------------------------------------------------- загрузка сессии

def find_rpp(path):
    """Файл .rpp или папка проекта Reaper -> путь к .rpp (в папке: имя как у папки, иначе самый свежий; до 2 уровней)."""
    if not os.path.isdir(path):
        return path
    path = os.path.abspath(path)
    found = []
    for dp, dirs, fns in os.walk(path):
        depth = dp[len(path):].count(os.sep)
        dirs[:] = [d for d in dirs if depth < 1 and d != 'Media']
        found += [os.path.join(dp, f) for f in fns if f.lower().endswith('.rpp') and not f.startswith('.')]
        if found:                                   # нашли на верхнем уровне — глубже не идём
            break
    if not found:
        raise ValueError('no .rpp project in folder: %s' % path)
    want = os.path.basename(path).lower() + '.rpp'
    mine = re.compile(r'_diff\d*\.rpp$', re.I)                   # сессии различий, сохранённые этой программой
    found.sort(key=lambda f: (os.path.basename(f).lower() != want, bool(mine.search(f)), -os.path.getmtime(f)))
    return found[0]


class Session:
    """Сессия Reaper: разобранные дорожки/item'ы/маркеры + сырой текст для записи."""

    def __init__(self, path):
        path = find_rpp(path)
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        self.proj = rpp.load(path)
        with open(path, encoding='utf-8', errors='replace', newline='') as f:
            text = f.read()
        self.eol = '\r\n' if '\r\n' in text else '\n'
        self.lines = text.splitlines()
        self.tree = parse_tree(self.lines)
        self.track_nodes = [c for c in self.tree.children if c.name == 'TRACK']
        self.track_guid = []
        for n in self.track_nodes:
            g = n.args[0] if n.args and n.args[0].startswith('{') else (n.line('TRACKID') or [''])[0]
            self.track_guid.append(g)
        self.items = []
        order = 0
        for ti, tn in enumerate(self.track_nodes):
            for c in tn.children:
                if c.name == 'ITEM':
                    self.items.append(self._item(self.proj.items[order], c))
                    order += 1
        self.markers = self._markers()

    def _item(self, it, node):
        it.node = node
        it.iguid = (node.line('IGUID') or [''])[0]
        it.lock = num_i(node.line('LOCK'))
        it.color = num_i(node.line('COLOR'))
        it.fadein_raw = tuple(node.line('FADEIN') or [])
        it.fadeout_raw = tuple(node.line('FADEOUT') or [])
        src = next((c for c in node.children if c.name == 'SOURCE'), None)
        it.kind = (src.args[0] if src and src.args else '')
        it.key = base(it.file) if it.file else ''
        it.end = it.pos + it.length
        it.other = self._other_lines(node)
        return it

    def _other_lines(self, node):
        out = []
        for ln in self.lines[node.start + 1:node.end]:
            t = ln.strip()
            if not t or t[0] in '<>':
                if t.startswith('<'):
                    out.append(t)
                continue
            tok = rpp.tokenize(t)
            if not tok or tok[0] in _IGNORE_KEYS:
                continue
            if tok[0] == 'FILE' and len(tok) > 1:
                tok = ['FILE', base(tok[1])] + tok[2:]
            out.append(' '.join(tok))
        return out

    def _markers(self):
        res = []
        for e in self.tree.entries:
            if e[0] != 'line' or e[1] != 'MARKER' or len(e[2]) < 4:
                continue
            t = e[2]
            idx, pos, name = int(rpp.num(t, 0)), rpp.num(t, 1), t[2]
            isreg = int(rpp.num(t, 3)) & 1
            if not isreg:
                for m in res:
                    if m['region'] and m['end'] < 0 and m['idx'] == idx:
                        m['end'] = pos
                        break
                else:
                    res.append(dict(idx=idx, pos=pos, name=name, region=False, end=-1.0,
                                    color=int(rpp.num(t, 4)), guid=t[7] if len(t) > 7 and t[7].startswith('{') else ''))
                continue
            res.append(dict(idx=idx, pos=pos, name=name, region=True, end=-1.0, color=int(rpp.num(t, 4)),
                            guid=t[7] if len(t) > 7 and t[7].startswith('{') else ''))
        for m in res:
            if m['region'] and m['end'] < 0:
                m['region'] = False
        return res

    def track_name(self, i):
        return self.proj.tracks[i].name if 0 <= i < len(self.proj.tracks) else '?'


def num_i(tok):
    return int(rpp.num(tok or [], 0))



# ---------------------------------------------------------------- дорожки: огибающие, плагины, запись

_FX_KINDS = ('VST', 'AU', 'CLAP', 'JS', 'DX', 'VIDEO_EFFECT', 'CONTAINER')
_ENV_LABEL = {'VOLENV': 'volume (pre-FX)', 'VOLENV2': 'volume', 'VOLENV3': 'trim volume', 'PANENV': 'pan (pre-FX)',
              'PANENV2': 'pan', 'MUTEENV': 'mute', 'WIDTHENV': 'width (pre-FX)', 'WIDTHENV2': 'width',
              'AUXVOLENV': 'send volume', 'AUXPANENV': 'send pan', 'AUXMUTEENV': 'send mute'}
_re_env = re.compile(r'^[A-Z_]*ENV\d?$')
_re_fx_prefix = re.compile(r'^(?:VST3i?|VSTi?|AUi?|CLAPi?|JS|DXi?):\s*', re.I)
_re_fx_vendor = re.compile(r'\s*\([^()]*\)\s*$')


def fx_chain(sess, node):
    """Цепочка FX из узла FXCHAIN / FXCHAIN_REC: [{name, kind, bypass, offline, wet, preset, state, params}]."""
    out = []
    byp = (0, 0)
    for e in node.entries:
        if e[0] == 'line':
            key, tok = e[1], e[2]
            if key == 'BYPASS':
                byp = (int(rpp.num(tok, 0)), int(rpp.num(tok, 1)))
            elif key == 'WET' and out and out[-1]['wet'] is None:
                out[-1]['wet'] = rpp.num(tok, 0, 1.0)
            elif key == 'PRESETNAME' and out and not out[-1]['preset']:
                out[-1]['preset'] = tok[0] if tok else ''
            continue
        ch = e[1]
        if ch.name not in _FX_KINDS:
            continue
        nm = ch.args[0] if ch.args else ''
        nm = _re_fx_vendor.sub('', _re_fx_prefix.sub('', nm)).strip()
        data, preset = [], ''
        for ln in sess.lines[ch.start + 1:ch.end]:
            t = ln.strip()
            if t.startswith('PRESETNAME'):
                tk = rpp.tokenize(t)
                preset = tk[1] if len(tk) > 1 else ''
            elif t:
                data.append(t)
        params = data[0].split() if ch.name == 'JS' and data else []
        out.append(dict(name=nm or ch.name, kind=ch.name, bypass=byp[0], offline=byp[1], wet=None, preset=preset,
                        state='\n'.join(data), params=params))
        byp = (0, 0)
    return out


def _fx_pair(x, y, label):
    ch = []
    n = '%s "%s"' % (label, x['name'])
    if x['bypass'] != y['bypass']:
        ch.append('%s: %s' % (n, 'bypassed' if y['bypass'] else 'enabled'))
    if x['offline'] != y['offline']:
        ch.append('%s: %s' % (n, 'set offline' if y['offline'] else 'back online'))
    if (x['wet'] or 1.0) != (y['wet'] or 1.0) and abs((x['wet'] or 1.0) - (y['wet'] or 1.0)) > 1e-4:
        ch.append('%s: wet %.0f%% -> %.0f%%' % (n, (x['wet'] or 1.0) * 100, (y['wet'] or 1.0) * 100))
    if x['preset'] != y['preset']:
        ch.append('%s: preset "%s" -> "%s"' % (n, x['preset'], y['preset']))
    if x['state'] != y['state']:
        if x['kind'] == 'JS' and x['params'] and y['params']:
            dif = ['slider %d: %s -> %s' % (k + 1, a, b) for k, (a, b) in enumerate(zip(x['params'], y['params'])) if a != b]
            ch.append('%s: parameters changed (%s)' % (n, ', '.join(dif[:6]) + (' …' if len(dif) > 6 else '')))
        else:
            ch.append('%s: settings changed' % n)
    return ch


def fx_diff(la, lb, label):
    na, nb = [f['name'] for f in la], [f['name'] for f in lb]
    if na != nb and sorted(na) == sorted(nb):
        return ['%s order: [%s] -> [%s]' % (label, ', '.join(na), ', '.join(nb))]
    ch = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                ch += _fx_pair(la[i1 + k], lb[j1 + k], label)
        else:
            ch += ['%s removed: "%s"' % (label, f['name']) for f in la[i1:i2]]
            ch += ['%s added: "%s"' % (label, f['name']) for f in lb[j1:j2]]
    return ch


def track_envelopes(sess, node, fx_names):
    """Огибающие дорожки (в т. ч. параметры FX, sends) -> {ключ: {act, pts, extra}}; пустые (без точек) пропускаются."""
    res = {}
    cnt = collections.Counter()

    def walk(n):
        for c in n.children:
            if c.name == 'ITEM':
                continue
            if c.name in ('FXCHAIN', 'FXCHAIN_REC'):
                walk(c)
            elif _re_env.match(c.name):
                pts = [(rpp.num(e[2], 0), rpp.num(e[2], 1), int(rpp.num(e[2], 2)), rpp.num(e[2], 3))
                       for e in c.entries if e[0] == 'line' and e[1] == 'PT' and len(e[2]) >= 2]
                if not pts:
                    continue
                label = _ENV_LABEL.get(c.name, c.name)
                if c.name == 'PARMENV':
                    a0 = c.args[0] if c.args else ''
                    head = a0.split(':')[0]
                    fxn = fx_names[int(head)] if head.isdigit() and int(head) < len(fx_names) else head
                    label = 'FX "%s" param %s' % (fxn, ':'.join(a0.split(':')[1:2]))
                cnt[label] += 1
                key = label if cnt[label] == 1 else '%s #%d' % (label, cnt[label])
                act = c.line('ACT')
                res[key] = dict(act=int(rpp.num(act or [], 0, 1)), pts=sorted(pts),
                                extra=(tuple(c.line('VOLTYPE') or []), tuple(c.line('DEFSHAPE') or [])))
    walk(node)
    return res


def env_diff(ea, eb, tol):
    ch = []
    for key in sorted(set(ea) | set(eb)):
        x, y = ea.get(key), eb.get(key)
        if x is None:
            ch.append('envelope "%s" added (%d points)' % (key, len(y['pts'])))
            continue
        if y is None:
            ch.append('envelope "%s" removed (%d points)' % (key, len(x['pts'])))
            continue
        if x['act'] != y['act']:
            ch.append('envelope "%s": %s' % (key, 'enabled' if y['act'] else 'bypassed'))
        if x['extra'] != y['extra']:
            ch.append('envelope "%s": mode/default shape changed' % key)
        gone, new = _pts_diff(x['pts'], y['pts'], tol), _pts_diff(y['pts'], x['pts'], tol)
        if gone or new:
            ts = [p[0] for p in gone + new]
            ch.append('envelope "%s": +%d/-%d points (%d -> %d), %s .. %s' % (
                key, len(new), len(gone), len(x['pts']), len(y['pts']), fmt_t(min(ts)), fmt_t(max(ts))))
    return ch


def _pts_diff(pa, pb, tol):
    """Точки из pa, которых нет в pb (время ±tol, значение, форма, натяжение); pb отсортирован по времени."""
    import bisect
    times = [p[0] for p in pb]
    out = []
    for p in pa:
        k = bisect.bisect_left(times, p[0] - tol)
        ok = False
        while k < len(pb) and pb[k][0] <= p[0] + tol:
            q = pb[k]
            if abs(q[1] - p[1]) <= 1e-6 + 1e-6 * abs(p[1]) and q[2] == p[2] and abs(q[3] - p[3]) <= 1e-4:
                ok = True
                break
            k += 1
        if not ok:
            out.append(p)
    return out


def _rec_input(v):
    if v < 0:
        return 'none'
    if v & 4096:
        return 'MIDI (%d)' % v
    if v & 1024:
        return 'stereo %d/%d' % ((v & 1023) + 1, (v & 1023) + 2)
    return 'mono %d' % (v + 1) if v < 512 else str(v)


def rec_diff(na, nb, arm=True):
    x, y = [int(rpp.num(na.line('REC') or [], i)) for i in range(12)], [int(rpp.num(nb.line('REC') or [], i)) for i in range(12)]
    ch = []
    if arm and x[0] != y[0]:
        ch.append('record arm: %s -> %s' % ('on' if x[0] else 'off', 'on' if y[0] else 'off'))
    if x[1] != y[1]:
        ch.append('record input: %s -> %s' % (_rec_input(x[1]), _rec_input(y[1])))
    if x[2] != y[2]:
        ch.append('input monitoring: %d -> %d' % (x[2], y[2]))
    if x[3] != y[3]:
        ch.append('record mode: %d -> %d' % (x[3], y[3]))
    if x[4:] != y[4:]:
        ch.append('record settings changed')
    return ch


def moved_tracks(pairs):
    """pairs: [(индекс A, индекс B)] по возрастанию A. Дорожки вне наибольшей общей возрастающей подпоследовательности
    (т. е. те, что действительно переставили, а не сдвинулись из-за добавленных/удалённых соседей)."""
    n = len(pairs)
    best, prev = [1] * n, [-1] * n
    for i in range(n):
        for k in range(i):
            if pairs[k][1] < pairs[i][1] and best[k] + 1 > best[i]:
                best[i], prev[i] = best[k] + 1, k
    if not n:
        return set()
    i = max(range(n), key=lambda k: best[k])
    keep = set()
    while i >= 0:
        keep.add(i)
        i = prev[i]
    return {pairs[k][0] for k in range(n) if k not in keep}


# ---------------------------------------------------------------- сравнение

class Differ:
    def __init__(self, a, b, tol=DEFAULT_TOL, progress=None, order=True, fx=True, env=True, state=True):
        self.a, self.b, self.tol = a, b, tol
        self.opt_order, self.opt_fx, self.opt_env, self.opt_state = order, fx, env, state      # что учитывать в дорожках
        self.progress = progress or (lambda *_: None)
        self.tmap = {}                         # индекс дорожки A -> индекс дорожки B
        self.track_diffs = []
        self.item_diffs = []
        self.marker_diffs = []
        self.project_diffs = []
        self.unchanged = 0

    def run(self):
        self.progress('Comparing tracks', 0, 4)
        self._tracks()
        self.progress('Comparing items', 1, 4)
        self._items()
        self.progress('Comparing markers', 2, 4)
        self._markers()
        self._project()
        self.progress('Comparing done', 4, 4)
        return self

    # ---- дорожки

    def _tracks(self):
        A, B = self.a, self.b
        fa, fb = set(range(len(A.proj.tracks))), set(range(len(B.proj.tracks)))
        gb = {}
        for j in fb:
            if B.track_guid[j]:
                gb.setdefault(B.track_guid[j], j)
        for i in sorted(fa):
            j = gb.get(A.track_guid[i])
            if j is not None and j in fb:
                self.tmap[i] = j
                fb.discard(j)
        fa -= set(self.tmap)
        byname = collections.defaultdict(list)
        for j in sorted(fb):
            byname[B.proj.tracks[j].name].append(j)
        for i in sorted(fa):
            lst = byname.get(A.proj.tracks[i].name)
            if lst:
                j = lst.pop(0)
                self.tmap[i] = j
                fb.discard(j)
        for i in sorted(fa - set(self.tmap)):
            self.track_diffs.append(dict(kind='removed', name=A.proj.tracks[i].name, changes=[], a_index=i, b_index=-1))
        for j in sorted(fb):
            self.track_diffs.append(dict(kind='added', name=B.proj.tracks[j].name, changes=[], a_index=-1, b_index=j))
        moved = moved_tracks(sorted(self.tmap.items())) if self.opt_order else set()
        for i, j in sorted(self.tmap.items()):
            ta, tb = A.proj.tracks[i], B.proj.tracks[j]
            ch = []
            fxa = fxb = []
            if ta.name != tb.name:
                ch.append('name: "%s" -> "%s"' % (ta.name, tb.name))
            if self.opt_state and ta.mute != tb.mute:
                ch.append('mute: %s -> %s' % (ta.mute, tb.mute))
            if self.opt_state and ta.solo != tb.solo:
                ch.append('solo: %s -> %s' % (ta.solo, tb.solo))
            if abs(ta.vol - tb.vol) > 1e-6:
                ch.append('volume: %s -> %s' % (db(ta.vol), db(tb.vol)))
            if abs(ta.pan - tb.pan) > 1e-4:
                ch.append('pan: %+.0f%% -> %+.0f%%' % (ta.pan * 100, tb.pan * 100))
            if ta.color != tb.color:
                ch.append('color changed')
            if ta.depth != tb.depth or ta.folder != tb.folder:
                ch.append('folder structure changed')
            na, nb = A.track_nodes[i], B.track_nodes[j]
            for cname, label in (('FXCHAIN', 'FX'), ('FXCHAIN_REC', 'input FX')):
                ca = next((c for c in na.children if c.name == cname), None)
                cb = next((c for c in nb.children if c.name == cname), None)
                la = fx_chain(A, ca) if ca is not None else []
                lb = fx_chain(B, cb) if cb is not None else []
                if self.opt_fx:
                    ch += fx_diff(la, lb, label)
                if cname == 'FXCHAIN':
                    fxa, fxb = [f['name'] for f in la], [f['name'] for f in lb]
            if len(ta.recvs) != len(tb.recvs) or [r[1:] for r in ta.recvs] != [r[1:] for r in tb.recvs]:
                ch.append('sends/receives changed')
            if self.opt_env:
                ch += env_diff(track_envelopes(A, na, fxa), track_envelopes(B, nb, fxb), self.tol)
            ch += rec_diff(na, nb, self.opt_state)
            if i in moved:
                ch.append('order: position %d -> %d' % (i + 1, j + 1))
            if ch:
                self.track_diffs.append(dict(kind='changed', name=tb.name, changes=ch, a_index=i, b_index=j))

    # ---- item'ы

    def _items(self):
        A, B, tol = self.a.items, self.b.items, self.tol
        tmap = self.tmap
        free_a, free_b = set(range(len(A))), set(range(len(B)))
        pairs = []

        def cost(i, j):
            d = abs(A[i].pos - B[j].pos)
            return (0 if d <= tol else 1, 0 if tmap.get(A[i].track) == B[j].track else 1, d)

        def assign(cands):
            cands.sort()
            for c, i, j in cands:
                if i in free_a and j in free_b:
                    pairs.append((i, j))
                    free_a.discard(i)
                    free_b.discard(j)

        def stage(keyfn, pred):
            bk = collections.defaultdict(list)
            for j in free_b:
                bk[keyfn(B[j])].append(j)
            cands = []
            for i in free_a:
                for j in bk.get(keyfn(A[i]), ()):
                    if pred(A[i], B[j]):
                        cands.append((cost(i, j), i, j))
            assign(cands)

        # 1. IGUID
        stage(lambda it: it.iguid or object(), lambda x, y: True)

        # 2. тот же файл и тот же диапазон источника (item переместили или не менялся)
        def same_src(x, y):
            if not x.key and tmap.get(x.track) != y.track:
                return False
            return (abs(x.soffs - y.soffs) <= tol and abs(x.length - y.length) <= tol and abs(x.rate - y.rate) < 1e-6)

        def key_nofile(it):
            return (it.key, it.kind) if it.key else ('', it.kind)
        stage(key_nofile, same_src)

        # 3. тот же файл, та же «синхронизация» на таймлайне, диапазоны пересекаются (обрезка)
        def sync(it):
            return it.pos - it.soffs / (it.rate or 1.0)

        def trimmed(x, y):
            return (x.key and abs(sync(x) - sync(y)) <= tol and x.pos < y.end + tol and y.pos < x.end + tol)
        stage(key_nofile, trimmed)

        # 4. тот же файл на той же дорожке, диапазоны источника пересекаются (обрезан и сдвинут)
        def overlap(x, y):
            if tmap.get(x.track) != y.track:
                return False
            if x.key:
                xa, xb = x.soffs, x.soffs + x.length * x.rate
                ya, yb = y.soffs, y.soffs + y.length * y.rate
            else:
                xa, xb, ya, yb = x.pos, x.end, y.pos, y.end
            return xa < yb and ya < xb
        stage(key_nofile, overlap)

        n = len(pairs)
        for k, (i, j) in enumerate(sorted(pairs)):
            ch, tags = self._compare(A[i], B[j])
            if ch:
                self.item_diffs.append(self._rec('changed', i, j, ch, tags))
            else:
                self.unchanged += 1
            if k % 100 == 0:
                self.progress('Comparing items', k, n)
        for i in sorted(free_a):
            self.item_diffs.append(self._rec('removed', i, -1, [], ['removed']))
        for j in sorted(free_b):
            self.item_diffs.append(self._rec('added', -1, j, [], ['added']))
        self.item_diffs.sort(key=lambda d: (d['pos'], d['track'], d['kind']))

    def _rec(self, kind, i, j, changes, tags):
        ia = self.a.items[i] if i >= 0 else None
        ib = self.b.items[j] if j >= 0 else None
        ref = ib or ia
        side = lambda it, s: None if it is None else dict(      # noqa: E731
            pos=it.pos, length=it.length, soffs=it.soffs, track=s.track_name(it.track), name=it.name, file=it.file)
        return dict(kind=kind, tags=tags, changes=changes, pos=ref.pos,
                    track=(self.b if ib else self.a).track_name(ref.track), name=ref.name or ref.key, file=ref.file,
                    a=side(ia, self.a), b=side(ib, self.b),
                    _ia=i, _ib=j)

    def _compare(self, x, y):
        tol = self.tol
        ch, tags = [], []

        def add(tag, text):
            ch.append(text)
            if tag not in tags:
                tags.append(tag)
        tb = self.b.track_name(y.track)
        if self.tmap.get(x.track) != y.track:
            add('track', 'track: "%s" -> "%s"' % (self.a.track_name(x.track), tb))
        if abs(x.pos - y.pos) > tol:
            add('moved', 'position: %s -> %s (%s)' % (fmt_t(x.pos), fmt_t(y.pos), fmt_dur(y.pos - x.pos)))
        if abs(x.length - y.length) > tol:
            add('trim', 'length: %.3f s -> %.3f s (%s)' % (x.length, y.length, fmt_dur(y.length - x.length)))
        if abs(x.soffs - y.soffs) > tol:
            add('trim', 'source offset: %.3f s -> %.3f s' % (x.soffs, y.soffs))
        if abs(x.rate - y.rate) > 1e-6 or abs(x.pitch - y.pitch) > 1e-6:
            add('rate', 'playrate/pitch: %.4g / %.2f -> %.4g / %.2f' % (x.rate, x.pitch, y.rate, y.pitch))
        if x.fadein_raw != y.fadein_raw and not self._same_fade(x.fadein_raw, y.fadein_raw):
            add('fade', 'fade in: %s -> %s' % (self._fade_s(x.fadein_raw), self._fade_s(y.fadein_raw)))
        if x.fadeout_raw != y.fadeout_raw and not self._same_fade(x.fadeout_raw, y.fadeout_raw):
            add('fade', 'fade out: %s -> %s' % (self._fade_s(x.fadeout_raw), self._fade_s(y.fadeout_raw)))
        if abs(x.vol - y.vol) > 1e-6:
            add('gain', 'volume: %s -> %s' % (db(x.vol), db(y.vol)))
        if x.mute != y.mute:
            add('mute', 'mute: %s -> %s' % (x.mute, y.mute))
        if x.lock != y.lock:
            add('other', 'lock: %d -> %d' % (x.lock, y.lock))
        if x.color != y.color:
            add('other', 'color changed')
        if x.name != y.name:
            add('name', 'name: "%s" -> "%s"' % (x.name, y.name))
        if x.key != y.key:
            add('source', 'source file: %s -> %s' % (os.path.basename(x.file.replace('\\', '/')) or '—',
                                                    os.path.basename(y.file.replace('\\', '/')) or '—'))
        if x.other != y.other:
            diff = [d for d in difflib.unified_diff(x.other, y.other, lineterm='', n=0)
                    if d[0] in '+-' and not d.startswith(('+++', '---'))]
            add('other', 'other properties: ' + ('; '.join(d[:80] for d in diff[:3]) + (' …' if len(diff) > 3 else '')))
        return ch, tags

    def _same_fade(self, p, q_):
        if len(p) != len(q_):
            return False
        for u, v in zip(p, q_):
            try:
                if abs(float(u) - float(v)) > max(self.tol, 1e-9):
                    return False
            except ValueError:
                if u != v:
                    return False
        return True

    @staticmethod
    def _fade_s(raw):
        if not raw:
            return '—'
        return '%.3f s (type %s)' % (max(rpp.num(raw, 1), rpp.num(raw, 2)), raw[0])

    # ---- маркеры

    def _markers(self):
        A, B, tol = self.a.markers, self.b.markers, self.tol
        free_a, free_b = set(range(len(A))), set(range(len(B)))
        pairs = []

        def take(cands):
            cands.sort()
            for c, i, j in cands:
                if i in free_a and j in free_b:
                    pairs.append((i, j))
                    free_a.discard(i)
                    free_b.discard(j)
        take([(abs(A[i]['pos'] - B[j]['pos']), i, j) for i in free_a for j in free_b
              if A[i]['guid'] and A[i]['guid'] == B[j]['guid']])
        take([(abs(A[i]['pos'] - B[j]['pos']), i, j) for i in free_a for j in free_b
              if A[i]['name'] == B[j]['name'] and A[i]['region'] == B[j]['region']])
        take([(abs(A[i]['pos'] - B[j]['pos']), i, j) for i in free_a for j in free_b
              if A[i]['region'] == B[j]['region'] and abs(A[i]['pos'] - B[j]['pos']) <= tol])
        for i, j in pairs:
            x, y = A[i], B[j]
            ch = []
            if x['name'] != y['name']:
                ch.append('name: "%s" -> "%s"' % (x['name'], y['name']))
            if abs(x['pos'] - y['pos']) > tol:
                ch.append('position: %s -> %s (%s)' % (fmt_t(x['pos']), fmt_t(y['pos']), fmt_dur(y['pos'] - x['pos'])))
            if y['region'] and abs(x['end'] - y['end']) > tol:
                ch.append('end: %s -> %s' % (fmt_t(x['end']), fmt_t(y['end'])))
            if ch:
                self.marker_diffs.append(dict(kind='changed', name=y['name'], pos=y['pos'], region=y['region'],
                                              end=y['end'], changes=ch, old_name=x['name'], old_pos=x['pos']))
        for i in sorted(free_a):
            m = A[i]
            self.marker_diffs.append(dict(kind='removed', name=m['name'], pos=m['pos'], region=m['region'], end=m['end'],
                                          changes=[], old_name=m['name'], old_pos=m['pos']))
        for j in sorted(free_b):
            m = B[j]
            self.marker_diffs.append(dict(kind='added', name=m['name'], pos=m['pos'], region=m['region'], end=m['end'],
                                          changes=[], old_name='', old_pos=-1))
        self.marker_diffs.sort(key=lambda d: d['pos'])

    # ---- проект

    def _project(self):
        pa, pb = self.a.proj, self.b.proj
        for label, x, y in (('sample rate', pa.sample_rate, pb.sample_rate), ('frame rate', pa.fps, pb.fps),
                            ('tempo', pa.bpm, pb.bpm), ('time signature', '%d/%d' % (pa.ts_num, pa.ts_den),
                                                        '%d/%d' % (pb.ts_num, pb.ts_den))):
            if x != y:
                self.project_diffs.append(dict(prop=label, a=str(x), b=str(y)))

    # ---- итог

    def result(self):
        cnt = collections.Counter(d['kind'] for d in self.item_diffs)
        tc = collections.Counter(d['kind'] for d in self.track_diffs)
        mc = collections.Counter(d['kind'] for d in self.marker_diffs)
        matched = cnt['changed'] + self.unchanged
        base = min(len(self.a.items), len(self.b.items))
        warning = ''
        if base >= 20 and matched / base < 0.15:
            warning = ('Only %d of %d items matched (%.0f%%): these sessions look like different projects, '
                       'so most differences below are not meaningful.' % (matched, base, 100.0 * matched / base))
        return dict(
            a=self.a.path, b=self.b.path, tol=self.tol, warning=warning,
            summary=dict(items_added=cnt['added'], items_removed=cnt['removed'], items_changed=cnt['changed'],
                         items_unchanged=self.unchanged, items_a=len(self.a.items), items_b=len(self.b.items),
                         tracks_added=tc['added'], tracks_removed=tc['removed'], tracks_changed=tc['changed'],
                         markers_added=mc['added'], markers_removed=mc['removed'], markers_changed=mc['changed']),
            project=self.project_diffs,
            tracks=self.track_diffs,
            items=[{k: v for k, v in d.items() if not k.startswith('_')} for d in self.item_diffs],
            markers=self.marker_diffs)


# ---------------------------------------------------------------- сессия различий

def _abs_media(path, src_dir):
    """Путь из RPP -> абсолютный (сессия различий лежит в другой папке)."""
    p = path.replace('\\', '/')
    is_win = len(p) > 2 and p[1] == ':'
    if p.startswith('/') or p.startswith('~') or p.startswith('//'):
        return path
    if is_win:
        if not os.path.exists(path):
            cand = os.path.join(src_dir, os.path.basename(p))
            if os.path.exists(cand):
                return cand
        return path
    return os.path.normpath(os.path.join(src_dir, p))


def _fix_item_lines(sess, node, new_ids, strip_lane, color, note_lines):
    """Строки item'а с абсолютными путями, цветом, заметкой и (для A) новыми GUID."""
    out = []
    lines = sess.lines[node.start:node.end + 1]
    direct_color = {sess_i for (e, sess_i) in zip(node.entries, node.eidx) if e[0] == 'line' and e[1] in ('COLOR',)}
    child_notes = next((c for c in node.children if c.name == 'NOTES'), None)
    src_child = next((c for c in node.children if c.name == 'SOURCE'), None)
    color_done = False
    for off, ln in enumerate(lines):
        gi = node.start + off
        t = ln.strip()
        if gi in direct_color:
            continue
        tok = rpp.tokenize(t) if t and t[0] not in '<>' else []
        key = tok[0] if tok else ''
        if strip_lane and key == 'YPOS':
            continue
        if new_ids and key in ('IGUID', 'GUID') and len(tok) > 1 and tok[1].startswith('{'):
            ln = ln[:len(ln) - len(ln.lstrip())] + '%s %s' % (key, new_guid())
        elif key == 'FILE' and len(tok) > 1:
            tail = ''.join(' ' + (q(x) if (' ' in x or not x) else x) for x in tok[2:])
            ln = ln[:len(ln) - len(ln.lstrip())] + 'FILE %s%s' % (q(_abs_media(tok[1], sess.dir)), tail)
        if not color_done and key == 'IGUID' and color is not None:
            out.append('      COLOR %d' % native_color(color))
            color_done = True
        if src_child is not None and gi == src_child.start and child_notes is None and note_lines:
            out.append('      <NOTES')
            out.extend('        |%s' % n for n in note_lines)
            out.append('      >')
        out.append(ln)
        if child_notes is not None and gi == child_notes.start and note_lines:
            out.extend('        |%s' % n for n in note_lines)
    if color is not None and not color_done:
        out.insert(1, '      COLOR %d' % native_color(color))
    return out


def _marker_line(idx, pos, name, region, color_rgb, end=None):
    col = native_color(color_rgb) if color_rgb is not None else 0
    out = ['  MARKER %d %.14g %s %d %d 1 B %s 0' % (idx, pos, q(name), 1 if region else 0, col, new_guid())]
    if region:
        out.append('  MARKER %d %.14g "" 0 %d 1 B %s 0' % (idx, end, col, new_guid()))
    return out


def build_diff_session(d, add=True, changed=True, removed=True, old=True, item_markers=True,
                       keep_unchanged_tracks=False):
    """Собирает .rpp с различиями (на основе B). Возвращает (текст, счётчики); на диск ничего не пишет."""
    A, B = d.a, d.b
    sel_b, notes_b = {}, {}           # индекс item'а B -> (цвет, строки заметки)
    a_items = collections.OrderedDict()    # индекс дорожки A -> [(цвет, заметка, item A)]
    markers = []                       # (pos, имя, цвет, region?, end)

    def short(n, s=60):
        return n if len(n) <= s else n[:s - 1] + '…'
    for r in d.item_diffs:
        k = r['kind']
        ia = A.items[r['_ia']] if r['_ia'] >= 0 else None
        ib = B.items[r['_ib']] if r['_ib'] >= 0 else None
        label = '%s: %s' % (r['track'], short(r['name']))
        if k == 'added' and add:
            sel_b[r['_ib']] = (COLORS['added'], ['[DIFF] ADDED'])
            if item_markers:
                markers.append((ib.pos, '+ ' + label, COLORS['added'], False, -1))
        elif k == 'changed':
            if changed:
                sel_b[r['_ib']] = (COLORS['changed'], ['[DIFF] CHANGED (B = new)'] + r['changes'])
                if item_markers:
                    markers.append((ib.pos, '~ ' + label, COLORS['changed'], False, -1))
            if old:
                a_items.setdefault(ia.track, []).append((COLORS['old'], ['[DIFF] OLD VERSION (A)'] + r['changes'], ia))
        elif k == 'removed' and removed:
            a_items.setdefault(ia.track, []).append((COLORS['removed'], ['[DIFF] REMOVED (exists only in A)'], ia))
            if item_markers:
                markers.append((ia.pos, '- ' + label, COLORS['removed'], False, -1))
    for m in d.marker_diffs:
        nm = {'added': '+ ', 'removed': '- ', 'changed': '~ '}[m['kind']] + m['name']
        if m['kind'] == 'changed' and m['old_name'] != m['name']:
            nm = '~ %s (was: %s)' % (m['name'], m['old_name'])
        col = {'added': COLORS['added'], 'removed': COLORS['removed'], 'changed': COLORS['changed']}[m['kind']]
        markers.append((m['pos'], '[M] ' + nm, col, m['region'], m['end']))
    markers.sort(key=lambda x: x[0])

    # --- построчная сборка на основе B
    lines = B.lines
    skip = set()
    repl = {}                          # номер строки -> новые строки (вместо строки)
    before = collections.defaultdict(list)
    for j, it in enumerate(B.items):
        node = it.node
        if j in sel_b:
            color, note = sel_b[j]
            repl[node.start] = _fix_item_lines(B, node, False, False, color, note)
            skip.update(range(node.start + 1, node.end + 1))
        else:
            skip.update(range(node.start, node.end + 1))
    root = B.tree
    kept_tracks = len(B.track_nodes)
    if not keep_unchanged_tracks:
        # дорожки без различий не переносим; у оставшихся убираем папки и sends (индексы дорожек сдвигаются)
        keep = {B.items[j].track for j in sel_b}
        keep |= {t['b_index'] for t in d.track_diffs if t['kind'] in ('added', 'changed')}
        kept_tracks = len(keep)
        for ti, tn in enumerate(B.track_nodes):
            if ti not in keep:
                skip.update(range(tn.start, tn.end + 1))
                continue
            for e, i in zip(tn.entries, tn.eidx):
                if e[0] == 'line' and e[1] == 'ISBUS':
                    repl[i] = ['    ISBUS 0 0']
                elif e[0] == 'line' and e[1] == 'AUXRECV':
                    skip.add(i)
                elif e[0] == 'child' and e[1].name in ('AUXVOLENV', 'AUXPANENV', 'AUXMUTEENV'):
                    skip.update(range(e[1].start, e[1].end + 1))
    first_track = next((c.start for c in root.children if c.name == 'TRACK'), root.end)
    for e, i in zip(root.entries, root.eidx):                         # исходные маркеры B убираем
        if e[0] == 'line' and e[1] == 'MARKER':
            skip.add(i)
    notes_node = next((c for c in root.children if c.name == 'NOTES'), None)
    summ = d.result()['summary']
    note_text = ['Differences session (generated by rpp_diff)',
                 'A (old): %s' % A.path, 'B (new): %s' % B.path,
                 'Items: +%d added, ~%d changed, -%d removed, %d unchanged' % (
                     summ['items_added'], summ['items_changed'], summ['items_removed'], summ['items_unchanged']),
                 'Green = added in B, yellow = changed (new version), red = removed from A (tracks "A > name"), grey = old version of changed items.',
                 'Item notes hold the description of the difference; markers "[M]" = marker/region differences.']
    block = ['  <NOTES 0 2'] + ['    |' + t for t in note_text] + ['  >']
    if notes_node is not None:
        repl[notes_node.start] = block
        skip.update(range(notes_node.start + 1, notes_node.end + 1))
    else:
        before[1].extend(block)
    mk = []
    for n, (pos, name, col, reg, end) in enumerate(markers, 1):
        mk.extend(_marker_line(n, pos, name, reg, col, end))
    before[first_track].extend(mk)

    # --- дорожки с A-item'ами в конец
    extra = []
    for ti, lst in a_items.items():
        tn = A.track_name(ti)
        g = new_guid()
        extra += ['  <TRACK %s' % g, '    NAME %s' % q('A > ' + tn), '    PEAKCOL %d' % native_color(COLORS['old']),
                  '    VOLPAN 1 0 -1 -1 1', '    MUTESOLO 0 0 0', '    ISBUS 0 0', '    NCHAN 2',
                  '    TRACKID %s' % g]
        for color, note, it in sorted(lst, key=lambda x: x[2].pos):
            extra.extend(_fix_item_lines(A, it.node, True, True, color, note))
        extra.append('  >')

    out = []
    for i, ln in enumerate(lines):
        if i == root.end:
            out.extend(extra)
        if i in before:
            out.extend(before[i])
        if i in repl:
            out.extend(repl[i])
        elif i not in skip:
            out.append(ln)
    if root.end >= len(lines):
        out.extend(extra)
    stats = dict(tracks_b=kept_tracks, items_b=len(sel_b), items_a=sum(len(v) for v in a_items.values()), markers=len(markers),
                tracks_a=len(a_items))
    return B.eol.join(out) + B.eol, stats


def write_diff_session(d, out_path, **kw):
    """Пишет .rpp с различиями на диск."""
    text, stats = build_diff_session(d, **kw)
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    return stats


def convert_to_pro_tools(d, text, out_dir, name, copy_media=True, extra_args=()):
    """Сессия различий (text) -> сессия Pro Tools движком «Reaper to Pro Tools» (reaper_to_ptx.py).
    Промежуточный .rpp на диск не пишется: проект регистрируется в памяти под путём рядом с B (для поиска медиа)."""
    if CONVERTER is None:
        raise SystemExit('Для сессии Pro Tools нужна программа «Reaper to Pro Tools» (в «Программах»)')
    import reaper_to_ptx
    virtual = os.path.join(d.b.dir, name + '.rpp')
    if os.path.exists(virtual):                       # в памяти имя не должно совпасть с настоящим файлом
        virtual = os.path.join(d.b.dir, name + ' (diff).rpp')
    rpp.register_virtual(virtual, text)
    argv = ['reaper_to_ptx.py', virtual, '--out', out_dir, '--name', name]
    if not copy_media:
        argv.append('--link')
    argv += list(extra_args)
    old = sys.argv
    sys.argv = argv
    try:
        reaper_to_ptx.main()
    finally:
        sys.argv = old


# ---------------------------------------------------------------- отчёт

def text_report(r):
    s = r['summary']
    o = ['A: %s' % r['a'], 'B: %s' % r['b'], '',
         'Items:   +%d added, ~%d changed, -%d removed, %d unchanged   (A: %d, B: %d)' % (
             s['items_added'], s['items_changed'], s['items_removed'], s['items_unchanged'], s['items_a'], s['items_b']),
         'Tracks:  +%d added, ~%d changed, -%d removed' % (s['tracks_added'], s['tracks_changed'], s['tracks_removed']),
         'Markers: +%d added, ~%d changed, -%d removed' % (s['markers_added'], s['markers_changed'], s['markers_removed'])]
    if r.get('warning'):
        o += ['', 'WARNING: ' + r['warning'], '']
    for p in r['project']:
        o.append('Project %s: %s -> %s' % (p['prop'], p['a'], p['b']))
    if r['tracks']:
        o += ['', '== Tracks']
        for t in r['tracks']:
            o.append('%s %s' % ({'added': '+', 'removed': '-', 'changed': '~'}[t['kind']], t['name']))
            o += ['    ' + c for c in t['changes']]
    if r['markers']:
        o += ['', '== Markers']
        for m in r['markers']:
            o.append('%s %s  %s' % ({'added': '+', 'removed': '-', 'changed': '~'}[m['kind']], fmt_t(m['pos']), m['name']))
            o += ['    ' + c for c in m['changes']]
    if r['items']:
        o += ['', '== Items']
        for it in r['items']:
            o.append('%s %s  [%s]  %s' % ({'added': '+', 'removed': '-', 'changed': '~'}[it['kind']], fmt_t(it['pos']),
                                          it['track'], it['name']))
            o += ['    ' + c for c in it['changes']]
    return '\n'.join(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Differences between two Reaper sessions')
    ap.add_argument('a', help='old session (.rpp or a Reaper project folder)')
    ap.add_argument('b', help='new session (.rpp or a Reaper project folder)')
    ap.add_argument('--tol', type=float, default=DEFAULT_TOL, help='time tolerance, seconds')
    ap.add_argument('--json', help='write the diff as JSON to this file')
    ap.add_argument('--report', action='store_true', help='print a text report')
    ap.add_argument('--out', help='save differences as a Reaper session (.rpp file or folder)')
    ap.add_argument('--no-added', action='store_true')
    ap.add_argument('--no-changed', action='store_true')
    ap.add_argument('--no-removed', action='store_true')
    ap.add_argument('--no-old', action='store_true', help='do not include old versions of changed items')
    ap.add_argument('--no-item-markers', action='store_true')
    ap.add_argument('--no-order', action='store_true', help='ignore track order')
    ap.add_argument('--no-fx', action='store_true', help='ignore plugins (FX chains)')
    ap.add_argument('--no-env', action='store_true', help='ignore track envelopes (automation)')
    ap.add_argument('--no-state', action='store_true', help='ignore track mute/solo/record arm')
    ap.add_argument('--pt', metavar='FOLDER', help='convert the differences straight to a Pro Tools session in this folder '
                                                  '(no intermediate .rpp is written)')
    ap.add_argument('--pt-name', help='Pro Tools session name (default: <B name> diff)')
    ap.add_argument('--pt-link', action='store_true', help='link to the original audio instead of copying it')
    ap.add_argument('--pt-option', action='append', default=[], help='extra reaper_to_ptx.py option, e.g. --pt-option=--no-fades')
    ap.add_argument('--keep-unchanged-tracks', action='store_true',
                    help='keep tracks without differences in the diff session (by default they are dropped)')
    args = ap.parse_args(argv)

    def prog(label, n, total):
        print('%s: %d/%d' % (label, n, total), flush=True)
    try:
        prog('Reading A', 0, 2)
        a = Session(args.a)
        prog('Reading B', 1, 2)
        b = Session(args.b)
    except (OSError, ValueError) as e:
        print('Error: %s' % e, file=sys.stderr)
        return 1
    d = Differ(a, b, args.tol, prog, order=not args.no_order, fx=not args.no_fx, env=not args.no_env, state=not args.no_state).run()
    res = d.result()
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False)
    if args.report:
        print(text_report(res))
    opts = dict(add=not args.no_added, changed=not args.no_changed, removed=not args.no_removed, old=not args.no_old,
                item_markers=not args.no_item_markers, keep_unchanged_tracks=args.keep_unchanged_tracks)
    if args.out:
        out = args.out
        if os.path.isdir(out) or not out.lower().endswith('.rpp'):
            os.makedirs(out, exist_ok=True)
            out = os.path.join(out, os.path.splitext(os.path.basename(b.path))[0] + '_diff.rpp')
        prog('Writing session', 0, 1)
        st = write_diff_session(d, out, **opts)
        prog('Writing session', 1, 1)
        print('Items from B: %d on %d tracks, from A: %d (tracks: %d), markers: %d' % (
            st['items_b'], st['tracks_b'], st['items_a'], st['tracks_a'], st['markers']))
        print('Done: %s' % out)
    if args.pt:
        text, st = build_diff_session(d, **opts)
        print('Items from B: %d on %d tracks, from A: %d (tracks: %d), markers: %d' % (
            st['items_b'], st['tracks_b'], st['items_a'], st['tracks_a'], st['markers']), flush=True)
        if not st['items_b'] and not st['items_a'] and not st['markers']:
            print('Error: no differences to convert', file=sys.stderr)
            return 1
        name = args.pt_name or (os.path.splitext(os.path.basename(b.path))[0] + ' diff')
        os.makedirs(args.pt, exist_ok=True)
        convert_to_pro_tools(d, text, args.pt, name, copy_media=not args.pt_link, extra_args=args.pt_option)
    return 0


if __name__ == '__main__':
    sys.exit(main())

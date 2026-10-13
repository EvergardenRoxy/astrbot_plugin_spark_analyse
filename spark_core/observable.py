"""Validated, bounded summary of an Observable (tasgon/observable) report, independent of AstrBot.

Observable measures the time each entity and block entity spends ticking, plus sampled Java stack traces for
them. It is a different tool from Spark: it has no TPS/MSPT, no thread tree and no GC data, so the summary is a
ranking of what costs tick time and where, which complements a Spark report of the same server.

The wire format is the JSON the mod uploads (and exports): `{"data": ProfilingData, "diagnostics": {...}}`,
or a bare ProfilingData for locally exported files. Everything is checked before use and every output list is
capped, so a hostile or oversized report cannot grow the model input.
"""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict

from .profile import ProfileError

OBSERVABLE_DECODED_LIMIT = 48*1024*1024
MAX_NODES = 1000000
MAX_DEPTH = 600
TICK_BUDGET_MS = 50.0
BAD = ProfileError('不是可分析的 Observable 报告（字段缺失或格式不符）')

SYSTEM_KEYS = re.compile(r'^(?:Minecraft Version(?: ID)?|Operating System|Java Version|Java VM Version|CPUs?|'
                         r'Processor Name|Memory|Max Memory|Total Memory|Allocated Memory|Physical Memory|'
                         r'Is Modded|Server Running|Type|Active Plugins|Launched Version)$', re.I)


def _number(value, allow_zero=True):
    """A finite non-negative number, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BAD
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise BAD
    return value


def _text(value, limit):
    if not isinstance(value, str):
        raise BAD
    return value[:limit]


def normalize_type(name):
    """Block timings carry a translation key (`block.minecraft.grass_block`); show it as a resource id."""
    name = str(name)[:120]
    match = re.fullmatch(r'(?:block|fluid)\.([a-z0-9_.-]+?)\.([a-z0-9_./-]+)', name)
    return f'{match.group(1)}:{match.group(2)}' if match else name


def parse_system_report(text):
    """Selected `Key: value` lines of Minecraft's system report; JVM flags are returned apart."""
    selected, flags = {}, None
    for line in str(text or '').splitlines():
        key, sep, value = line.strip().partition(': ')
        if not sep:
            continue
        if key == 'JVM Flags':
            flags = value[:4000]
        elif SYSTEM_KEYS.match(key) and key not in selected and len(selected) < 16:
            selected[key] = value[:200]
    return selected, flags


class _Frames:
    """Self-sample counts of stack frames over any number of trace trees; the node limit is shared."""
    def __init__(self):
        self.nodes = 0

    def walk(self, root, into):
        """Add the self count of every frame under root to `into`; returns root's count."""
        if not isinstance(root, dict):
            raise BAD
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            if not isinstance(node, dict) or depth > MAX_DEPTH:
                raise ProfileError('Observable 调用栈深度超限或格式不符')
            self.nodes += 1
            if self.nodes > MAX_NODES:
                raise ProfileError('调用节点超过限额：Observable 报告的调用栈超过 100 万个节点，通常是采样时间太长。请缩短采样时间后重新采集')
            count = _number(node.get('count'))
            children = node.get('children', [])
            if not isinstance(children, list):
                raise BAD
            below = 0
            for child in children:
                if not isinstance(child, dict):
                    raise BAD
                below += _number(child.get('count'))
                stack.append((child, depth+1))
            frame = _text(node.get('className', ''), 200)+'.'+_text(node.get('methodName', ''), 100)
            into[frame] += max(0, count-below)
        return _number(root.get('count'))


def _top_frames(counter, total, n):
    return [{'frame': frame[:160], 'samples': int(count), 'pct': round(100*count/total, 1)}
            for frame, count in counter.most_common(n) if count > 0 and total]


class ObservableProfile:
    def __init__(self, raw):
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw)
        except (ValueError, RecursionError):
            raise BAD from None
        if not isinstance(document, dict):
            raise BAD
        # The server upload wraps the data with diagnostics; exported files are the bare data.
        data = document.get('data') if isinstance(document.get('data'), dict) else document
        diagnostics = document.get('diagnostics') if isinstance(document.get('diagnostics'), dict) else {}
        if not isinstance(data.get('entities'), dict) or not isinstance(data.get('blocks'), dict):
            raise BAD
        self.ticks = int(_number(data.get('ticks'), allow_zero=False))
        self.diagnostics = diagnostics
        self.entities = self._entries(data['entities'], 'entities')
        self.blocks = self._entries(data['blocks'], 'blocks')
        self.trace = data.get('traces')
        if not self.entities and not self.blocks:
            raise ProfileError('Observable 报告没有任何实体或方块的耗时记录')
        self.frames = _Frames()

    def _entries(self, groups, label):
        """Flat rows (dimension, type, x, y, z, ns_per_server_tick, own_ticks, ns_per_own_tick, trace)."""
        rows, count = [], 0
        for dimension, entries in groups.items():
            if not isinstance(entries, list):
                raise BAD
            for entry in entries:
                if not isinstance(entry, dict):
                    raise BAD
                count += 1
                if count > 500000:
                    raise ProfileError('Observable 报告的条目超过 50 万个，通常是采样时间太长')
                position = entry.get('position')
                if not isinstance(position, dict):
                    raise BAD
                rate, own = _number(entry.get('rate')), int(_number(entry.get('ticks')))
                coordinates = tuple(position.get(axis) for axis in 'xyz')
                if any(isinstance(c, bool) or not isinstance(c, int) for c in coordinates):
                    raise BAD
                kind = _text(entry.get('type', ''), 120)
                rows.append((str(dimension)[:80], normalize_type(kind) if label == 'blocks' else kind,
                             *coordinates, rate*own/self.ticks, own, rate, entry.get('traces')))
        return rows

    def overview(self):
        diagnostics = self.diagnostics
        extra = diagnostics.get('additionalDiagnostics') if isinstance(diagnostics.get('additionalDiagnostics'), dict) else {}
        system, flags = parse_system_report(extra.get('System Report'))
        mods = [m.strip()[:100] for m in str(extra.get('Mods') or '').splitlines() if m.strip()]
        duration = diagnostics.get('duration')
        start = diagnostics.get('start')
        valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        duration, start = (duration if valid(duration) else None), (start if valid(start) else None)
        tps = min(20.0, round(self.ticks*1000/duration, 2)) if duration else None
        rows = [('entity', *r) for r in self.entities] + [('block', *r) for r in self.blocks]
        total_ns = sum(r[6] for r in rows)
        ms = lambda ns: round(ns/1e6, 3)
        share = lambda ns: round(100*ns/total_ns, 1) if total_ns else None

        def by_type(kind):
            groups = defaultdict(lambda: [0, 0.0, 0.0])
            for r in rows:
                if r[0] == kind:
                    group = groups[r[2]]
                    group[0] += 1
                    group[1] += r[6]
                    group[2] = max(group[2], r[8])
            ranked = sorted(groups.items(), key=lambda item: -item[1][1])
            return [{'type': name, 'entries': g[0], 'ms_per_tick': ms(g[1]), 'share_pct': share(g[1]),
                     'worst_single_us_per_own_tick': round(g[2]/1e3, 1)} for name, g in ranked[:12]], len(groups)

        dimensions = defaultdict(lambda: {'entity': 0.0, 'block': 0.0, 'n': 0})
        chunks = defaultdict(lambda: [0.0, 0, Counter()])
        for r in rows:
            d = dimensions[r[1]]
            d[r[0]] += r[6]
            d['n'] += 1
            chunk = chunks[(r[1], r[3]//16, r[5]//16)]
            chunk[0] += r[6]
            chunk[1] += 1
            chunk[2][r[2]] += r[6]
        hot = sorted(rows, key=lambda r: -r[6])[:10]
        entity_types, entity_type_count = by_type('entity')
        block_types, block_type_count = by_type('block')
        # Per-type sampled call stacks: the frames where each costly type spends its samples.
        wanted = {t['type'] for t in entity_types[:5]} | {t['type'] for t in block_types[:5]}
        per_type = defaultdict(Counter)
        totals = Counter()
        server = Counter()
        server_total = self.frames.walk(self.trace, server) if isinstance(self.trace, dict) else 0
        for r in sorted(rows, key=lambda r: -r[6]):
            if r[9] is not None and r[2] in wanted:
                totals[r[2]] += self.frames.walk(r[9], per_type[r[2]])
        trace = None
        if server_total or totals:
            trace = {'server_samples': int(server_total), 'server_top_self_frames': _top_frames(server, server_total, 8),
                     'by_type': {name: {'samples': int(totals[name]), 'top_self_frames': _top_frames(per_type[name], totals[name], 5)}
                                 for name in sorted(per_type, key=lambda n: -totals[n]) if totals[name]},
                     'note': '栈采样只覆盖被选中计时的对象，次数是采样点数，不是毫秒；类名/方法名可能是混淆名，需要映射才能对应到模组'}
        loader = diagnostics.get('modLoader')
        overview = {
            'kind': 'observable', 'fingerprint': self.fingerprint, 'parser': 'observable-0.1',
            'runtime': {'minecraft': _opt(diagnostics.get('minecraftVersion'), 40),
                        'mod_loader': _opt(loader, 40), 'observable_version': _opt(diagnostics.get('observableVersion'), 40),
                        'start_ms': start, 'duration_ms': duration, 'end_ms': start+duration if start and duration else None,
                        'ticks': self.ticks, 'tps_estimate': tps, 'system': system,
                        'jvm_flags': flags, 'mod_count': len(mods), 'mods': mods[:120], 'mods_truncated': len(mods) > 120},
            'totals': {'observed_ms_per_tick': ms(total_ns), 'tick_budget_pct': round(100*total_ns/1e6/TICK_BUDGET_MS, 1),
                       'entity_ms_per_tick': ms(sum(r[6] for r in rows if r[0] == 'entity')),
                       'block_ms_per_tick': ms(sum(r[6] for r in rows if r[0] == 'block')),
                       'entity_entries': len(self.entities), 'block_entries': len(self.blocks)},
            'dimensions': [{'dimension': name, 'ms_per_tick': ms(d['entity']+d['block']), 'entity_ms': ms(d['entity']),
                            'block_ms': ms(d['block']), 'entries': d['n']}
                           for name, d in sorted(dimensions.items(), key=lambda i: -(i[1]['entity']+i[1]['block']))[:8]],
            'entity_types': entity_types, 'entity_type_count': entity_type_count,
            'block_types': block_types, 'block_type_count': block_type_count,
            'hot_entries': [{'kind': r[0], 'type': r[2], 'dimension': r[1], 'x': r[3], 'y': r[4], 'z': r[5],
                             'ms_per_tick': ms(r[6]), 'ticked': r[7], 'us_per_own_tick': round(r[8]/1e3, 1)} for r in hot],
            'hot_chunks': [{'dimension': k[0], 'chunk_x': k[1], 'chunk_z': k[2], 'block_x': k[1]*16+8, 'block_z': k[2]*16+8,
                            'ms_per_tick': ms(v[0]), 'entries': v[1], 'top_types': [[t, ms(c)] for t, c in v[2].most_common(3)]}
                           for k, v in sorted(chunks.items(), key=lambda i: -i[1][0])[:8]],
            'trace': trace,
            'limitations': ['只统计实体、方块实体和方块/流体随机刻的耗时，不含区块加载、网络、世界保存等其他 tick 开销',
                            '没有 TPS/MSPT 记录；tps_estimate 由 ticks 与时长估算',
                            '每个对象的耗时是它自己被 tick 时的平均值（ms_per_tick 已折算为每个服务器 tick 的平均占用）',
                            '耗时在服务端 tick 线程上串行相加，可与 50 毫秒预算对照']}
        return overview

    def query(self, **_):
        raise ProfileError('Observable 报告不提供调用树查询')


def _opt(value, limit):
    return value[:limit] if isinstance(value, str) else None

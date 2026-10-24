"""Bounded numeric chart export. No model, network, plotting or AstrBot dependency."""
import heapq
import json
import math
from collections import defaultdict
from .profile import ProfileError, present_number, present_count

MAX_POINTS = 2000
MAX_INPUT_POINTS = 1_000_000
MAX_BUNDLE_BYTES = 2*1024*1024


def label(value, limit=80):
    # Plot text literally; report strings must never enter mathtext/TeX or filenames.
    return ''.join(c for c in str(value) if c.isprintable() and c not in '$\\')[:limit]


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def bounded_points(points, limit=MAX_POINTS):
    """Keep endpoints and per-bucket extrema, never average percentiles. None separates gaps."""
    if len(points) <= limit:
        return points
    buckets = max(1, (limit-2)//7)
    result = [points[0]]
    for b in range(buckets):
        lo = 1 + b*(len(points)-2)//buckets
        hi = 1 + (b+1)*(len(points)-2)//buckets
        ids = range(lo, hi)
        valid = [i for i in ids if points[i][1] is not None]
        selected = set()
        if valid:
            selected.update((min(valid, key=lambda i: points[i][1]), max(valid, key=lambda i: points[i][1])))
        missing = next((i for i in range(lo, hi) if points[i][1] is None), None)
        if missing is not None:
            selected.add(missing)
        for i in sorted(selected):
            point = points[i]
            if missing is not None and point[1] is not None:
                # A gapped bucket cannot support a continuous line. Keep extrema as isolated markers.
                result.extend(([point[0], None], point, [point[0], None]))
            else:
                result.append(point)
    return result + [points[-1]]


def viewer_source(data, node):
    if node.class_name.startswith('com.destroystokyo.paper.event.executor.asm.generated.'):
        return None
    source = None
    if node.method_name and node.method_desc:
        source = data.method_sources.get(node.class_name+';'+node.method_name+';'+node.method_desc)
    if not source and node.line_number:
        source = data.line_sources.get(node.class_name+';'+str(node.line_number))
    source = source or data.class_sources.get(node.class_name)
    if source in ('java', 'minecraft'):
        return None
    if (source and 'carpet' in source and node.class_name.startswith('net.minecraft.server.MinecraftServer')
            and node.method_name.endswith(('modifiedRunLoop', 'fixUpdateSuppressionCrashTick'))):
        return None
    return source or None


def source_ranking(profile):
    data = profile.data
    m = data.metadata
    p = m.platform_metadata
    kind = (p.name+' '+p.brand).casefold()
    title = '插件来源 Top 7' if any(x in kind for x in ('bukkit', 'paper', 'leaf', 'spigot', 'purpur')) else (
        '模组来源 Top 7' if any(x in kind for x in ('forge', 'fabric', 'quilt')) else '代码来源 Top 7')
    if any(x in kind for x in ('arclight', 'mohist', 'magma', 'catserver')):
        title = '代码来源 Top 7'
    candidates = [t for t in data.threads if t.name == 'Server thread']
    if (p.type != 0 or 'folia' in kind or len(candidates) != 1 or
            m.data_aggregator.thread_grouper == 2 or
            (m.data_aggregator.thread_grouper == 1 and not
             (m.thread_dumper.type == 1 and len(m.thread_dumper.ids) == 1))):
        return {'title': title, 'rows': [], 'reason': '没有可确认的独立 Server thread；不以最忙线程代替'}
    thread = candidates[0]
    denominator = sum(thread.times)
    totals, active = defaultdict(float), defaultdict(int)
    stack = [(i, False, None) for i in reversed(thread.children_refs)]
    while stack:
        index, exiting, saved = stack.pop()
        if exiting:
            if saved:
                active[saved] -= 1
            continue
        node = thread.children[index]
        source = viewer_source(data, node)
        if source:
            if not active[source]:
                totals[source] += sum(node.times)
            active[source] += 1
        stack.append((index, True, source))
        stack.extend((i, False, None) for i in reversed(node.children_refs))
    rows = []
    for source, value in heapq.nlargest(7, totals.items(), key=lambda item: item[1]):
        if value <= 0 or denominator <= 0:
            continue
        meta = m.sources.get(source)
        rows.append({'name': label(meta.name if meta and meta.name else source),
                     'source': label(source), 'sampled_ms': value, 'pct': 100*value/denominator})
    return {'title': title, 'thread': thread.name, 'denominator_ms': denominator, 'rows': rows,
            'scope': '全部保留调用窗口', 'reason': '' if rows else '报告没有可用的第三方来源映射'}


def export(profile):
    data, m = profile.data, profile.data.metadata
    p = m.platform_metadata
    warnings, series = [], {}
    count = sum(len(message.values) for _, message in m.metrics.ListFields() if hasattr(message, 'values'))
    if count > MAX_INPUT_POINTS:
        raise ProfileError('绘图遥测点数超过 100 万，未生成图表')
    # Explicit fields only; never export user identity, configuration, report IDs, JVM args or paths.
    definitions = (
        ('tick_duration', ('median', 'percentile95', 'max'), 'tick'),
        ('tps', (None,), 'tps'), ('world_info', ('players',), 'players'),
        ('memory_usage_heap', ('used', 'committed', 'max'), 'heap'),
        ('memory_usage_non_heap', ('used', 'committed'), 'nonheap'),
        ('player_ping', ('median', 'percentile95', 'max'), 'ping'))
    for field, keys, prefix in definitions:
        msg = getattr(m.metrics, field)
        if not msg.values:
            continue
        if len(msg.timestamp_deltas_ms) != len(msg.values) or msg.start_timestamp_ms <= 0:
            warnings.append(label(field+' 时间数组无效'))
            continue
        now, times = msg.start_timestamp_ms, []
        valid = True
        for delta in msg.timestamp_deltas_ms:
            now += delta
            if now > 253402300799000 or (times and now <= times[-1]):
                valid = False
                break
            times.append(now)
        if not valid:
            warnings.append(label(field+' 时间顺序无效'))
            continue
        for key in keys:
            points = []
            for timestamp, value in zip(times, msg.values):
                v = present_number(value, key) if key else value
                v = v if finite(v) and v >= 0 else None
                points.append([timestamp, v])
            # Retain explicit gaps plus gaps in recording cadence. Do not bridge missing intervals.
            if len(times) > 2:
                diffs = sorted(b-a for a, b in zip(times, times[1:]))
                typical = diffs[len(diffs)//2]
                gapped = []
                for i, point in enumerate(points):
                    if i and point[0]-points[i-1][0] > max(typical*3, 1000):
                        gapped.append([points[i-1][0]+1, None])
                    gapped.append(point)
                points = gapped
            name = prefix + ('_'+key if key else '')
            if prefix == 'players':
                name = 'players'
            if any(v is not None for _, v in points):
                series[name] = {'points': bounded_points(points), 'origin': 'metrics',
                                'downsampled': len(points) > MAX_POINTS}

    fallback = {'tick_median': 'mspt_median', 'tick_max': 'mspt_max', 'tps': 'tps', 'players': 'players'}
    unanchored = False
    for name, field in fallback.items():
        if name in series:
            continue
        points = []
        previous_end = None
        skipped = False
        for w in sorted(profile.windows):
            stat = data.time_window_statistics.get(w)
            if stat is None:
                skipped = True
                continue
            if stat.start_time <= 0 or stat.end_time <= stat.start_time:
                unanchored = True
                skipped = True
                continue
            if points and (skipped or (previous_end and stat.start_time > previous_end+1000)):
                points.append([stat.start_time-1, None])
            v = present_count(stat, field) if field == 'players' else present_number(stat, field)
            points.append([(stat.start_time+stat.end_time)//2, v if finite(v) and v >= 0 else None])
            previous_end, skipped = stat.end_time, False
        if any(v is not None for _, v in points):
            series[name] = {'points': bounded_points(sorted(points)), 'origin': 'window',
                            'downsampled': len(points) > MAX_POINTS}
    if unanchored:
        warnings.append('部分窗口没有可信绝对时间，未伪造时间轴')
    def usage(msg):
        return {key: present_count(msg, key) for key in ('used', 'committed', 'max')}
    def gc_map(mapping):
        return [{'name': label(name), 'count': present_count(q, 'total'),
                 'avg_ms': present_count(q, 'avg_time'), 'interval_ms': present_count(q, 'avg_frequency')}
                for name, q in sorted(mapping.items())[:16]]
    pools = [{'name': label(q.name), **usage(q.usage)} for q in m.platform_statistics.memory.pools[:8]]
    target = m.platform_statistics.tps.game_target_tps
    target = target if target > 0 else 20
    budget = m.platform_statistics.mspt.game_max_ideal_mspt
    sampling = {'start_ms': m.start_time, 'end_ms': m.end_time or None,
                'target_tps': target, 'tick_budget_ms': budget if budget > 0 else 1000/target}
    if profile.inconsistent_windows:
        warnings.append('局部窗口采样误差：整段聚合有效，异常窗口不提供调用树定量查询')
    bounds = [point[0] for q in series.values() for point in q['points']]
    result = {'schema_version': 1,
              'platform': {'name': label(p.name), 'brand': label(p.brand), 'minecraft': label(p.minecraft_version),
                           'type': p.type, 'java': label(m.system_statistics.java.version)},
              'sampling': {key: sampling[key] for key in ('start_ms', 'end_ms', 'target_tps', 'tick_budget_ms')},
              'range': [min(bounds), max(bounds)] if bounds else None,
              'series': series, 'heap': usage(m.platform_statistics.memory.heap),
              'nonheap': usage(m.platform_statistics.memory.non_heap), 'pools': pools,
              'ping': {key: present_number(m.platform_statistics.ping.last15m, key)
                       for key in ('min', 'median', 'mean', 'percentile95', 'max')},
              'gc': {'platform': gc_map(m.platform_statistics.gc), 'system': gc_map(m.system_statistics.gc)},
              'sources': source_ranking(profile), 'warnings': warnings[:12]}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_BUNDLE_BYTES:
        raise ProfileError('绘图数据超过 2 MiB，未生成图表')
    return result

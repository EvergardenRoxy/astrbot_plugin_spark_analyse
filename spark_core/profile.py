"""Validated, bounded Spark execution-profile queries, independent of AstrBot."""
import hashlib
import heapq
import math
from .proto.spark_sampler_pb2 import SamplerData

SCHEMA = '03210f75c3b040f1bf7b369761573b83c75bb5ce'

class ProfileError(ValueError):
    pass


def present_number(message, name):
    # proto3 scalar zero has no presence; do not invent a measured zero.
    return getattr(message, name) if name in {f.name for f, _ in message.ListFields()} else None


def present_count(message, name):
    # Spark writes -1 when it could not count; that is unknown, not a count.
    value = present_number(message, name)
    return value if value is None or value >= 0 else None


# JDK frames where a thread is parked rather than running. Counted once at the outermost frame, so native
# frames below them (async-profiler) are included.
WAIT_FRAMES = frozenset({'jdk.internal.misc.Unsafe.park', 'sun.misc.Unsafe.park', 'java.lang.Thread.sleep',
                         'java.lang.Thread.sleep0', 'java.lang.Thread.sleepNanos0', 'java.lang.Object.wait',
                         'java.lang.Object.wait0'})
PERFORMANCE_GAME_RULES = ('randomTickSpeed', 'doMobSpawning', 'maxEntityCramming', 'spawnChunkRadius')


class Profile:
    def __init__(self, raw, max_nodes=1000000):
        self.data = SamplerData.FromString(raw)
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        m = self.data.metadata
        if not self.data.HasField('metadata') or not self.data.threads:
            raise ProfileError('不是可分析的 sampler 报告')
        if m.sampler_mode != 0 or m.sampler_engine not in (0, 1):
            raise ProfileError('首版仅支持 execution Java/async 报告，不支持 allocation/lock')
        self.windows = list(self.data.time_windows)
        if not self.windows or len(set(self.windows)) != len(self.windows):
            raise ProfileError('时间窗口缺失或重复')
        self.parents = []
        count = 0
        for t in self.data.threads:
            count += len(t.children)
            if count > max_nodes:
                raise ProfileError('调用节点超过限额：报告的调用树超过 100 万个节点，通常是采样时间太长。请缩短采样时间后重新上传，例如 /spark profiler start --timeout 300（5 分钟后自动停止）')
            parents = [-2] * len(t.children)
            for i, n in [(-1, t), *enumerate(t.children)]:
                if len(n.times) != len(self.windows) or any(not math.isfinite(x) or x < 0 for x in n.times):
                    raise ProfileError('采样数值或窗口长度非法')
                for c in n.children_refs:
                    if c < 0 or c >= len(parents) or parents[c] != -2 or c == i:
                        raise ProfileError('非法、重复或循环节点引用')
                    parents[c] = i
            if -2 in parents:
                raise ProfileError('存在不可达调用节点')
            # Each chain must reach its thread root; also cap malicious depth.
            depths = {}
            for i in range(len(parents)):
                path, current = [], i
                while current != -1 and current not in depths:
                    path.append(current)
                    if len(path) > 512:
                        raise ProfileError('调用深度超限或循环引用')
                    current = parents[current]
                depth = depths.get(current, 0)
                for current in reversed(path):
                    depth += 1
                    if depth > 512:
                        raise ProfileError('调用深度超限')
                    depths[current] = depth
            for n in [t, *t.children]:
                for wi, total in enumerate(n.times):
                    children_total = sum(t.children[c].times[wi] for c in n.children_refs)
                    if children_total > total + max(0.001, total*1e-6):
                        raise ProfileError('报告内部数据不一致（子调用耗时大于父调用），无法得到可信占比，已拒绝分析以免给出错误结论；请用最新版Spark重新采样')
            self.parents.append(parents)

    def runtime_metadata(self):
        from google.protobuf.json_format import MessageToDict
        m = self.data.metadata
        truncations = []
        budget = [24000]
        def bounded(value, path):
            if isinstance(value, dict):
                result = {}
                for i, (k, v) in enumerate(value.items()):
                    if i >= 32 or budget[0] <= 0:
                        truncations.append(path)
                        break
                    key = str(k)[:120]
                    budget[0] -= len(key)
                    result[key] = bounded(v, path+'.'+key)
                return result
            if isinstance(value, list):
                if len(value)>32: truncations.append(path)
                return [bounded(v, path) for v in value[:32] if budget[0]>0]
            if isinstance(value, str):
                size = min(12000, max(0, budget[0]))
                if len(value)>size: truncations.append(path)
                budget[0] -= min(len(value), size)
                return value[:size]
            return value
        def convert(message):
            return bounded(MessageToDict(message, preserving_proto_field_name=True), message.DESCRIPTOR.name)
        def gc_map(values, path):
            result = {}
            for i, (key, value) in enumerate(values.items()):
                if i >= 32 or budget[0] <= 0:
                    truncations.append(path)
                    break
                if len(key) > 120:
                    truncations.append(path)
                bounded_key = bounded(key[:120], path)
                result[bounded_key] = convert(value)
            return result
        system = m.system_statistics
        platform = m.platform_statistics
        hardware = {'os': convert(system.os), 'cpu': convert(system.cpu),
                    'java': convert(system.java), 'jvm': convert(system.jvm),
                    'memory': convert(system.memory)}
        # Hardware is only a heuristic cohort; omit Java so upgrading it does not change the label.
        identity = '|'.join([system.os.name[:120], system.os.arch[:40], system.os.version[:120],
                             system.cpu.model_name[:200], str(system.cpu.threads)])
        known = bool(system.os.name or system.cpu.model_name)
        tag = 'auto-'+hashlib.sha256(identity.encode()).hexdigest()[:12] if known else ''
        return {'system': hardware, 'heap': convert(platform.memory),
                'platform_gc': gc_map(platform.gc, 'platform_gc'),
                'system_gc': gc_map(system.gc, 'system_gc'),
                'server_hint': {'tag': tag, 'basis': identity if known else None,
                                'confidence': 'heuristic_not_unique',
                                'warning': '同配置机器可能碰撞；非服务器唯一ID'},
                'truncated_fields': truncations,
                'limitations': ['GC averages are not pause timeline', 'physical RAM is not container memory limit']}

    def overview(self):
        m = self.data.metadata
        p = m.platform_metadata
        s = m.platform_statistics
        return {'fingerprint': self.fingerprint, 'schema': SCHEMA, 'parser': '0.1.0',
                'runtime': self.runtime_metadata(),
                'platform': {'type': p.type, 'name': p.name[:240], 'version': p.version[:240],
                             'minecraft': p.minecraft_version[:120], 'spark': p.spark_version[:120],
                             'brand': p.brand[:120],
                             'data_version': p.spark_data_version},
                'sampling': {'mode': 'execution', 'engine': m.sampler_engine,
                             'interval_us': m.interval, 'start_ms': m.start_time,
                             'end_ms': m.end_time or None,
                             'aggregator': m.data_aggregator.type,
                             'tick_threshold': m.data_aggregator.tick_length_threshold,
                             'included_ticks': m.data_aggregator.number_of_included_ticks},
                'health': {'tps_1m': present_number(s.tps, 'last1m'),
                           'mspt_median': present_number(s.mspt.last1m, 'median'),
                           'mspt_p95': present_number(s.mspt.last1m, 'percentile95'),
                           'mspt_max': present_number(s.mspt.last1m, 'max'),
                           'cpu_process_1m': present_number(m.system_statistics.cpu.process_usage, 'last1m')},
                'threads': [{'id': i, 'name': t.name[:120], 'inclusive_ms': sum(t.times),
                             'nodes': len(t.children)} for i, t in enumerate(self.data.threads)][:100],
                'threads_truncated': len(self.data.threads) > 100,
                'windows': self.windows[:120], 'windows_truncated': len(self.windows) > 120,
                'window_health': [{'window': w, 'tps': present_number(s, 'tps'),
                                   'mspt_median': present_number(s, 'mspt_median'),
                                   'mspt_max': present_number(s, 'mspt_max'),
                                   'players': present_count(s, 'players'),
                                   'entities': present_count(s, 'entities'),
                                   'tile_entities': present_count(s, 'tile_entities'),
                                   'chunks': present_count(s, 'chunks')}
                                  for w, s in sorted(self.data.time_window_statistics.items())][:120],
                'world': self.world_statistics(),
                'platform_truncated': any(len(value) > limit for value, limit in (
                     (p.name, 240), (p.version, 240), (p.minecraft_version, 120), (p.spark_version, 120),
                     (p.brand, 120))),
                 'limitations': ['scalar absent/zero reported as null', 'no automatic obfuscation mapping',
                                'class source only; not mod causality', 'thread share is not CPU share']}

    def world_statistics(self):
        """Entity counts Spark stores with the report (absent on older reports); bounded summaries only."""
        stats = self.data.metadata.platform_statistics
        if not stats.HasField('world'):
            return None
        w = stats.world
        def top(counts, n):
            return [[str(k)[:80], v] for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]
        chunks = ((world.name, c) for world in w.worlds for region in world.regions for c in region.chunks)
        busiest = heapq.nlargest(5, chunks, key=lambda item: item[1].total_entities)
        rules = {}
        for rule in w.game_rules:
            if rule.name in PERFORMANCE_GAME_RULES:
                changed = {k[:80]: v[:40] for k, v in sorted(rule.world_values.items()) if v != rule.default_value}
                if changed:
                    rules[rule.name] = {'default': rule.default_value[:40], 'worlds': dict(list(changed.items())[:16])}
        return {'total_entities': present_count(w, 'total_entities'),
                'entity_type_count': len(w.entity_counts), 'entity_types': top(w.entity_counts, 10),
                'dimensions': [{'name': world.name[:80], 'entities': world.total_entities}
                               for world in sorted(w.worlds, key=lambda x: -x.total_entities)[:12]],
                'dimension_count': len(w.worlds),
                'busiest_chunks': [{'dimension': name[:80], 'chunk_x': c.x, 'chunk_z': c.z,
                                    'block_x': c.x*16+8, 'block_z': c.z*16+8, 'entities': c.total_entities,
                                    'entity_types': top(c.entity_counts, 3)} for name, c in busiest],
                'changed_game_rules': rules}

    def waiting(self, thread):
        """Sampled ms the thread spent parked. Whether that is spare time between ticks or a stall inside a tick
        is judged from MSPT (briefing.triage), not from method names, which differ across platforms and versions."""
        t = self.data.threads[thread]
        parents = self.parents[thread]
        inside = [None] * len(t.children)
        total = 0.0
        for start in range(len(t.children)):
            chain, i = [], start
            while i != -1 and inside[i] is None:
                chain.append(i)
                i = parents[i]
            waiting = inside[i] if i != -1 else False
            for j in reversed(chain):
                n = t.children[j]
                is_wait = n.class_name+'.'+n.method_name in WAIT_FRAMES
                if is_wait and not waiting:
                    total += sum(n.times)
                waiting = waiting or is_wait
                inside[j] = waiting
        return total

    def evidence_pack(self):
        """Bounded representative self hotspots; never sum inclusive ancestors."""
        selected = sorted(range(len(self.data.threads)), key=lambda i: (
            self.data.threads[i].name not in ('Server thread', 'Render thread'),
            -sum(self.data.threads[i].times)))[:4]
        threads = []
        for ti in selected:
            t = self.data.threads[ti]
            result = self.query(thread=ti)
            hotspots = [r for r in result['rows'][:5] if r['self_ms'] > 0]
            for r in hotspots:
                ids, current = [], r['node']
                while current != -1:
                    ids.append(current)
                    current = self.parents[ti][current]
                ids.reverse()
                # Preserve root and immediate caller context without a 512-frame prompt.
                path_ids = ids if len(ids) <= 8 else ids[:2] + ids[-6:]
                r['path'] = [{'node': i, 'method': (t.children[i].class_name+'.'+t.children[i].method_name)[:160]}
                             for i in path_ids]
                r['path_omitted_frames'] = max(0, len(ids)-8)
            covered = sum(r['self_ms'] for r in hotspots)
            total = result['denominator_ms']
            threads.append({'thread': ti, 'name': t.name[:120], 'window': None,
                            'denominator_ms': total, 'wait_ms': self.waiting(ti),
                            'selected_self_ms': covered,
                            'selected_self_coverage_pct': 100*covered/total if total else None,
                            'hotspots': hotspots, 'remaining_sampled_ms': max(0, total-covered)})
        return {'unit': 'sampled_ms', 'threads': threads,
                'omitted_threads': len(self.data.threads)-len(selected),
                'limits': {'threads': 4, 'hotspots_per_thread': 5, 'path_frames': 8},
                'coverage_meaning': 'selected disjoint node self / thread total; not causal confidence or CPU share',
                'scope': 'all recorded windows; temporal findings require window query'}

    def query(self, view='hotspots', thread=0, node=-1, window=None, search='', offset=0):
        if not isinstance(thread, int) or not 0 <= thread < len(self.data.threads):
            raise ProfileError('线程ID无效')
        t = self.data.threads[thread]
        if window is not None and window not in self.windows:
            raise ProfileError('窗口ID无效')
        wi = self.windows.index(window) if window is not None else None
        def value(n):
            return sum(n.times) if wi is None else n.times[wi]
        total = value(t)
        if not isinstance(node, int) or not -1 <= node < len(t.children):
            raise ProfileError('节点ID无效')
        if not isinstance(offset, int) or not 0 <= offset <= len(t.children):
            raise ProfileError('分页offset无效')
        def row(i):
            n = t.children[i]
            inc = value(n)
            own = inc - sum(value(t.children[c]) for c in n.children_refs)
            if own < -max(0.001, inc * 1e-6):
                raise ProfileError('子调用采样超过父调用，拒绝输出错误self')
            return {'node': i, 'method': (n.class_name + '.' + n.method_name)[:240],
                    'parent': self.parents[thread][i], 'inclusive_ms': inc, 'self_ms': max(0, own),
                    'inclusive_pct': 100 * inc / total if total else None,
                    'self_pct': 100 * max(0, own) / total if total else None,
                    'source': self.data.class_sources.get(n.class_name, '')[:120] or None,
                    'source_evidence': 'class_mapping_only', 'line': n.line_number or None}
        if view == 'hotspots':
            # Keep bounded output, not bounded evidence: ranking visits every node.
            import heapq
            ids = range(len(t.children))
            rows = heapq.nlargest(offset + 21, (row(i) for i in ids), key=lambda r: r['self_ms'])[offset:]
        elif view == 'children':
            n = t if node == -1 else t.children[node]
            rows = sorted((row(i) for i in n.children_refs), key=lambda r: r['inclusive_ms'], reverse=True)[offset:offset+21]
        elif view == 'callers':
            ids = []
            while node != -1:
                ids.append(node)
                node = self.parents[thread][node]
            rows = [row(i) for i in reversed(ids)][offset:offset+21]
        elif view == 'search':
            if not search or len(search) > 120:
                raise ProfileError('搜索必须为1–120字符的字面文本')
            ids = (i for i, n in enumerate(t.children) if search.casefold() in (n.class_name+'.'+n.method_name).casefold())
            import itertools
            rows = [row(i) for i in itertools.islice(ids, offset, offset+21)]
        else:
            raise ProfileError('不支持的查询view')
        return {'fingerprint': self.fingerprint, 'thread': thread, 'window': window,
                'unit': 'sampled_ms', 'denominator_ms': total,
                'filter': 'none', 'rows': rows[:20], 'next_offset': offset+20 if len(rows)>20 else None}

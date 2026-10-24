"""What the analysis model is given: load classes, triage signals, a trimmed overview, and the prompt text.

Pure functions, independent of AstrBot. Thresholds live only here; the prompt files refer to the computed
labels instead of repeating the numbers, so the two cannot drift apart.
"""
import copy
import hashlib
import json
import re

# Maintainer-set load classes. Mild overload is common on busy multi-world servers (vanilla ticks every
# dimension in turn on one thread), so it is reported but not treated as a fault.
SUSTAINED_TPS, SUSTAINED_MSPT = 15, 70
MILD_TPS, MILD_MSPT = 19.5, 40
SPIKE_RATIO, SPIKE_MIN_MS = 5, 500
# Default only. Spark reports can specify a different target tick rate and ideal tick budget.
# A rolling median cannot establish whether parked samples occurred inside or between ticks.
TICK_BUDGET_MS = 50

# Observable cost classes: the share of the 50 ms tick budget that entities and block entities alone use.
OBS_HEAVY_PCT, OBS_NOTABLE_PCT = 50, 20
# A type (or three) taking at least this share of the measured cost is a concentrated source.
OBS_DOMINANT_PCT, OBS_CONCENTRATED_PCT = 40, 70
# Spark and Observable reports are treated as one server when versions agree and the sampling windows are this
# close (or overlap). Further apart they may still be the same server, but not under the same conditions.
PAIR_NEAR_SECONDS = 1800

JVM_QUESTION = re.compile(r'JVM|GC|垃圾回收|启动参数|堆内存|Xm[sx]|vm.?args', re.I)
KEY_VM_ARG = re.compile(r'-(?:Xm[sx]|Xss|XX:[+-]?Use\w*GC\b|XX:[+-]?ZGenerational|XX:(?:Max|Initial|Min)RAMPercentage'
                        r'|XX:MaxGCPauseMillis|XX:[+-]?UseCompactObjectHeaders)', re.I)

# SHA-256 of whitespace-stripped reply prompts that shipped as the default. A saved config equal to one of
# them was never customised, so it follows the current default.
LEGACY_REPLY_DIGESTS = frozenset({'bd585aebaf7f1add846fbc63f4f19c2e1e1a0ec0c973676ef75f0bc6b1d13709',  # up to 1.0.4
                                  '85e75d8c7d8d2f14b07340053cfffadbc71f548c49c511a4855e1a2ab37717ce'})  # unreleased draft


def _digest(text):
    return hashlib.sha256(''.join(str(text).split()).encode()).hexdigest()


def is_legacy_reply(text):
    return bool(str(text or '').strip()) and _digest(text) in LEGACY_REPLY_DIGESTS


def reply_requirements(configured, default):
    text = str(configured or '').strip()
    return default if not text or is_legacy_reply(text) else text


def system_prompt(policy, guide, reply):
    return policy.strip()+'\n\n'+guide.strip()+'\n\n回复要求：\n'+reply.strip()


def _round(value, digits=1):
    return None if value is None else round(value, digits)


def _pct(part, total):
    return round(100*part/total, 1) if part is not None and total else None


def _largest(rows, key):
    return max((row[key] for row in rows if row.get(key) is not None), default=None)


def _load(tps, median, target, budget):
    """Keep the existing 20 TPS classes, scaled to the report's actual Minecraft tick target."""
    if tps is None and median is None:
        return 'unknown'
    if (tps is not None and tps < target*SUSTAINED_TPS/20) or (median is not None and median > budget*SUSTAINED_MSPT/50):
        return 'sustained'
    if (tps is not None and tps < target*MILD_TPS/20) or (median is not None and median > budget*MILD_MSPT/50):
        return 'mild'
    return 'none'


def _spikes(median, peak):
    return None if peak is None or median is None else peak > SPIKE_MIN_MS and peak > SPIKE_RATIO*median


def triage(overview):
    health = overview.get('health') or {}
    sampling = overview.get('sampling') or {}
    target = sampling.get('target_tps') or 20
    budget = sampling.get('tick_budget_ms') or 1000/target
    server = (overview.get('platform') or {}).get('type') == 0
    tps, median, peak = health.get('tps_1m'), health.get('mspt_median'), health.get('mspt_max')
    load = _load(tps, median, target, budget) if server else 'unknown'
    spikes = _spikes(median, peak) if server else None
    threads = (overview.get('evidence_pack') or {}).get('threads') or []
    tick = next((t for t in threads if server and t.get('role') == 'server_tick'), {})
    regions = [t for t in threads if server and t.get('role') == 'region_tick']
    wait = _pct(tick.get('wait_ms'), tick.get('denominator_ms'))
    filtered = sampling.get('aggregator') == 1 or (sampling.get('tick_threshold') or 0) > 0
    wait_reason = 'filtered_ticks' if filtered else 'thread_role_unknown' if not tick else 'sampling_scope_not_proven'
    windows = overview.get('window_health') or []
    window_summary = overview.get('window_summary') or {}
    # Keep counts from one snapshot; separately maximising players and chunks invents a non-existent ratio.
    chunk_window = window_summary.get('peak_chunks') or max(
        (w for w in windows if w.get('chunks') is not None), key=lambda w: w['chunks'], default={})
    players, chunks = chunk_window.get('players'), chunk_window.get('chunks')
    entities = (overview.get('world') or {}).get('total_entities')
    if entities is None:
        entities = _largest(windows, 'entities')
    per_player = round(chunks/players, 1) if chunks is not None and players else None
    start, end = sampling.get('start_ms'), sampling.get('end_ms')
    worst_tps = (window_summary.get('worst_tps') or {}).get('tps')
    worst_median = (window_summary.get('worst_median') or {}).get('mspt_median')
    window_load = _load(worst_tps, worst_median, target, budget) if server else 'unknown'
    peak_window = window_summary.get('peak_mspt') or {}
    window_spikes = _spikes(peak_window.get('mspt_median'), peak_window.get('mspt_max')) if server else None
    signals = {'load': load, 'spikes': spikes, 'tps': _round(tps, 2), 'mspt_median': _round(median),
               'mspt_p95': _round(health.get('mspt_p95')), 'mspt_max': _round(peak),
               'spike_ratio': round(peak/median, 1) if peak is not None and median else None,
               'metric_scope': 'last_1m_at_report_end', 'server_applicable': server,
               'target_tps': target, 'tick_budget_ms': budget,
               'tick_thread': tick.get('name'), 'wait_pct': wait, 'wait_kind': 'unknown',
               'wait_reason': wait_reason, 'filtered_ticks': filtered,
               'region_threads': [{'thread': t['thread'], 'name': t['name'],
                                   'wait_pct': _pct(t.get('wait_ms'), t.get('denominator_ms'))} for t in regions],
               'window_load': window_load,
               'window_spikes': window_spikes, 'peak_window_mspt': peak_window.get('mspt_max'),
               'worst_window_tps': worst_tps, 'worst_window_mspt_median': worst_median,
               'players': players, 'chunks': chunks, 'chunks_per_player': per_player, 'entities': entities,
               'sample_seconds': round((end-start)/1000, 1) if start and end and end > start else None,
               'thresholds': {'sustained': f'TPS<{target*SUSTAINED_TPS/20:g} 或 MSPT中位数>{budget*SUSTAINED_MSPT/50:g}毫秒',
                              'mild': f'TPS<{target*MILD_TPS/20:g} 或 MSPT中位数>{budget*MILD_MSPT/50:g}毫秒（未达持续过载）',
                              'spike': f'MSPT最大值>{SPIKE_MIN_MS}毫秒且>{SPIKE_RATIO}倍中位数'}}
    signals['summary'] = _summary(signals)
    return signals


def _summary(s):
    metrics = '，'.join(part for part in (f"TPS {s['tps']}" if s['tps'] is not None else '',
                                          f"MSPT中位数 {s['mspt_median']} 毫秒" if s['mspt_median'] is not None else '') if part)
    parts = [{'none': f'最近一分钟未见持续过载（{metrics}）',
              'mild': f'轻微过载（{metrics}），多人多维度服务器常见',
              'sustained': f'持续过载（{metrics}）',
              'unknown': '报告缺少 TPS/MSPT 或并非服务端，无法判断服务端负载'}[s['load']]]
    if s['load'] in ('mild', 'sustained'):
        parts[0] = '最近一分钟'+parts[0]
    if s['server_applicable']:
        parts.append(f"目标 {s['target_tps']:g} TPS，每 tick 预算 {s['tick_budget_ms']:g} 毫秒")
    if s['spikes']:
        parts.append(f"最近一分钟存在偶发尖峰：最慢一次 tick {s['mspt_max']:.0f} 毫秒，约为中位数的 {s['spike_ratio']} 倍")
    elif s['spikes'] is False:
        parts.append('最近一分钟未见明显尖峰')
    if s['window_load'] in ('mild', 'sustained'):
        parts.append(f"采样窗口曾记录负载升高（最低 TPS {s['worst_window_tps']}，最高窗口 MSPT 中位数 {s['worst_window_mspt_median']} 毫秒；可能来自不同窗口），需核对对应时段")
    if s['window_spikes']:
        parts.append(f"采样窗口曾记录长 tick：{s['peak_window_mspt']:g} 毫秒，不能由结束时最近一分钟正常排除")
    if s['filtered_ticks']:
        parts.append('调用树只保留筛选的慢 tick，不能把它的等待占比解释成整段服务器空闲率')
    wait = s['wait_pct']
    if wait is not None and wait >= 1:
        parts.append(f'tick 线程约 {wait}% 的采样为等待；滚动 MSPT 不能证明这些等待发生在 tick 之间还是 tick 内，需核对调用路径和同期窗口')
    if s['region_threads']:
        parts.append('Folia 使用多个区域 tick 线程；各线程等待比例分别记录，不能合并成服务器空闲率')
    if s['chunks'] is not None and s['players']:
        parts.append(f"{s['players']} 名玩家，已加载 {s['chunks']} 个区块（每人约 {s['chunks_per_player']:.0f} 个）")
    if s['entities'] is not None:
        parts.append(f"实体 {s['entities']} 个")
    return '；'.join(parts)+'。'


def summarize_vm_args(text):
    flags = text.split()
    key = [flag[:120] for flag in flags if KEY_VM_ARG.match(flag)][:16]
    return {'key_flags': key, 'other_flag_count': len(flags)-len(key),
            'note': '其余启动参数已省略；用户询问 JVM、GC 或启动参数时会提供全文'}


def model_overview(overview, question):
    view = copy.deepcopy(overview)
    java = ((view.get('runtime') or {}).get('system') or {}).get('java') or {}
    args = java.get('vm_args')
    # The full argument line is often several KB and only matters for JVM questions.
    if isinstance(args, str) and args and not JVM_QUESTION.search(question or ''):
        java['vm_args'] = summarize_vm_args(args)
    return {'triage': triage(overview), **view}


def observable_triage(view):
    """Signals for an Observable overview, from the same kind of fixed rules as the Spark triage."""
    runtime, totals = view.get('runtime') or {}, view.get('totals') or {}
    tps, budget = runtime.get('tps_estimate'), totals.get('tick_budget_pct')
    if tps is None:
        load = 'unknown'
    elif tps < SUSTAINED_TPS:
        load = 'sustained'
    elif tps < MILD_TPS:
        load = 'mild'
    else:
        load = 'none'
    cost = 'unknown' if budget is None else 'heavy' if budget >= OBS_HEAVY_PCT else 'notable' if budget >= OBS_NOTABLE_PCT else 'light'
    ranked = sorted((view.get('entity_types') or []) + (view.get('block_types') or []), key=lambda row: -(row.get('share_pct') or 0))
    top3 = round(sum(row.get('share_pct') or 0 for row in ranked[:3]), 1)
    lead = ranked[0] if ranked else None
    chunk = (view.get('hot_chunks') or [None])[0]
    chunk_share = round(100*chunk['ms_per_tick']/totals['observed_ms_per_tick'], 1) if chunk and totals.get('observed_ms_per_tick') else None
    signals = {'load': load, 'cost': cost, 'tps_estimate': tps, 'observed_ms_per_tick': totals.get('observed_ms_per_tick'),
               'budget_pct': budget, 'top_type': lead['type'] if lead else None, 'top_type_share_pct': lead.get('share_pct') if lead else None,
               'top3_share_pct': top3, 'dominant_type': bool(lead and (lead.get('share_pct') or 0) >= OBS_DOMINANT_PCT),
               'concentrated': top3 >= OBS_CONCENTRATED_PCT, 'hot_chunk_share_pct': chunk_share,
               'thresholds': {'cost': f'实体与方块实体耗时占 {TICK_BUDGET_MS} 毫秒 tick 预算：>={OBS_HEAVY_PCT}% 为 heavy，>={OBS_NOTABLE_PCT}% 为 notable',
                              'load': f'TPS 估算值（由 tick 数与时长算出）：<{SUSTAINED_TPS} sustained，<{MILD_TPS} mild',
                              'dominant': f'单一类型占已测耗时 >={OBS_DOMINANT_PCT}%；前三类合计 >={OBS_CONCENTRATED_PCT}% 视为集中'}}
    signals['summary'] = _observable_summary(signals)
    return signals


def _observable_summary(s):
    parts = []
    if s['tps_estimate'] is not None:
        parts.append({'none': f"采样期间 TPS 估算约 {s['tps_estimate']}，未见持续过载", 'mild': f"采样期间 TPS 估算约 {s['tps_estimate']}，轻微过载",
                      'sustained': f"采样期间 TPS 估算约 {s['tps_estimate']}，持续过载", 'unknown': ''}[s['load']])
    if s['budget_pct'] is not None:
        parts.append({'light': f"实体和方块实体合计每个 tick 约占 {s['observed_ms_per_tick']:.2f} 毫秒（约占 tick 预算的 {s['budget_pct']}%），负担不重",
                      'notable': f"实体和方块实体合计每个 tick 约占 {s['observed_ms_per_tick']:.2f} 毫秒（约占 tick 预算的 {s['budget_pct']}%），占比可观",
                      'heavy': f"实体和方块实体合计每个 tick 约占 {s['observed_ms_per_tick']:.2f} 毫秒（约占 tick 预算的 {s['budget_pct']}%），是 tick 的主要开销",
                      'unknown': ''}[s['cost']])
    if s['top_type']:
        parts.append(f"最耗时的类型是 {s['top_type']}（占已测耗时 {s['top_type_share_pct']}%）" +
                     ('，耗时集中在少数类型' if s['concentrated'] else '，耗时比较分散'))
    return '；'.join(p for p in parts if p)+'。'


def _norm_loader(value):
    text = str(value or '').casefold()
    for name in ('neoforge', 'forge', 'fabric', 'quilt'):
        if name in text:
            return name
    return None


def correlate(spark, observable):
    """Do a Spark and an Observable overview describe the same server at about the same time?

    Pure comparison of what both reports record: Minecraft version, mod loader and sampling windows. The verdict
    only says whether merging is plausible; it cannot prove identity (two servers can run the same version).
    """
    platform, runtime = spark.get('platform') or {}, observable.get('runtime') or {}
    mc_spark, mc_obs = str(platform.get('minecraft') or ''), str(runtime.get('minecraft') or '')
    minecraft = 'unknown' if not mc_spark or not mc_obs else 'same' if mc_spark == mc_obs else 'different'
    loader_obs = _norm_loader(runtime.get('mod_loader'))
    loader_spark = _norm_loader(' '.join(str(platform.get(k) or '') for k in ('name', 'brand')))
    if not loader_obs or not loader_spark:
        # Hybrid servers (Arclight, Mohist, ...) report as Bukkit while running a mod loader, so this stays unknown.
        loader = 'unknown'
    else:
        loader = 'same' if loader_obs == loader_spark else 'different'
    sampling = spark.get('sampling') or {}
    s_start, s_end = sampling.get('start_ms'), sampling.get('end_ms')
    o_start, o_end = runtime.get('start_ms'), runtime.get('end_ms')
    gap = overlap = None
    if s_start and s_end and o_start and o_end:
        gap = max(0, max(s_start, o_start)-min(s_end, o_end))/1000
        overlap = gap == 0
    if minecraft == 'different' or loader == 'different':
        verdict = 'conflict'
    elif overlap:
        verdict = 'overlapping'
    elif gap is not None and gap <= PAIR_NEAR_SECONDS:
        verdict = 'near'
    elif gap is not None:
        verdict = 'apart'
    else:
        verdict = 'time_unknown'
    result = {'verdict': verdict, 'minecraft': minecraft, 'mod_loader': loader,
              'sampling_gap_seconds': round(gap) if gap is not None else None, 'sampling_windows_overlap': overlap,
              'near_means': f'采样时间间隔不超过 {PAIR_NEAR_SECONDS//60} 分钟'}
    median = (spark.get('health') or {}).get('mspt_median')
    observed = (observable.get('totals') or {}).get('observed_ms_per_tick')
    if verdict != 'conflict' and median and observed is not None:
        result['observed_ms_vs_spark_mspt_median_pct'] = round(100*observed/median, 1)
    return result


def model_observable(view, question):
    """Observable overview for the model: triage first, JVM flags summarised unless the question is about the JVM."""
    view = copy.deepcopy(view)
    runtime = view.get('runtime') or {}
    flags = runtime.get('jvm_flags')
    if isinstance(flags, str) and flags and not JVM_QUESTION.search(question or ''):
        runtime['jvm_flags'] = summarize_vm_args(flags)
    return {'triage': observable_triage(view), **view}


def user_payload(question, overview, comparison=None, previous=None, comparison_unavailable=None, *, observable=None, pairing=None):
    """The user message for the model. Without an Observable report the shape is the Spark-only one, unchanged."""
    payload = {'user_observation': question[:2000]}
    if observable is not None:
        payload['report_kind'] = 'spark+observable' if overview is not None else 'observable'
    if overview is not None:
        payload['overview'] = model_overview(overview, question)
    if observable is not None:
        payload['observable'] = model_observable(observable, question)
        payload['pairing'] = pairing
    payload.update({'history_comparison': comparison,
                    # Why a requested comparison was not made; the plugin also tells the user.
                    'history_comparison_unavailable': comparison_unavailable,
                    'previous_review_untrusted': previous[:6000] if comparison and previous else None})
    return json.dumps(payload, ensure_ascii=False)

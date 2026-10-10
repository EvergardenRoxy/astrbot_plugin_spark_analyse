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
# A tick has a 50 ms budget. A median tick well inside it leaves the thread waiting for the next tick (spare
# capacity); a median at or over it leaves no time between ticks, so waits happen inside ticks. MSPT decides
# this on every platform, unlike method names; TPS is not used because spikes alone can lower it.
TICK_BUDGET_MS, IDLE_WAIT_BELOW_MSPT = 50, 40

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


def triage(overview):
    health = overview.get('health') or {}
    tps, median, peak = health.get('tps_1m'), health.get('mspt_median'), health.get('mspt_max')
    if tps is None and median is None:
        load = 'unknown'
    elif (tps is not None and tps < SUSTAINED_TPS) or (median is not None and median > SUSTAINED_MSPT):
        load = 'sustained'
    elif (tps is not None and tps < MILD_TPS) or (median is not None and median > MILD_MSPT):
        load = 'mild'
    else:
        load = 'none'
    spikes = None if peak is None or median is None else peak > SPIKE_MIN_MS and peak > SPIKE_RATIO*median
    threads = (overview.get('evidence_pack') or {}).get('threads') or []
    tick = threads[0] if threads else {}
    wait = _pct(tick.get('wait_ms'), tick.get('denominator_ms'))
    if median is None:
        wait_kind = 'unknown'
    elif median < IDLE_WAIT_BELOW_MSPT:
        wait_kind = 'between_ticks'
    elif median >= TICK_BUDGET_MS:
        wait_kind = 'in_tick'
    else:
        wait_kind = 'unknown'
    windows = overview.get('window_health') or []
    players, chunks = _largest(windows, 'players'), _largest(windows, 'chunks')
    entities = (overview.get('world') or {}).get('total_entities')
    if entities is None:
        entities = _largest(windows, 'entities')
    per_player = round(chunks/players, 1) if chunks is not None and players else None
    sampling = overview.get('sampling') or {}
    start, end = sampling.get('start_ms'), sampling.get('end_ms')
    signals = {'load': load, 'spikes': spikes, 'tps': _round(tps, 2), 'mspt_median': _round(median),
               'mspt_p95': _round(health.get('mspt_p95')), 'mspt_max': _round(peak),
               'spike_ratio': round(peak/median, 1) if peak is not None and median else None,
               'tick_thread': tick.get('name'), 'wait_pct': wait, 'wait_kind': wait_kind,
               'players': players, 'chunks': chunks, 'chunks_per_player': per_player, 'entities': entities,
               'sample_seconds': round((end-start)/1000, 1) if start and end and end > start else None,
               'thresholds': {'sustained': f'TPS<{SUSTAINED_TPS} 或 MSPT中位数>{SUSTAINED_MSPT}毫秒',
                              'mild': f'TPS<{MILD_TPS} 或 MSPT中位数>{MILD_MSPT}毫秒（未达持续过载）',
                              'spike': f'MSPT最大值>{SPIKE_MIN_MS}毫秒且>{SPIKE_RATIO}倍中位数'}}
    signals['summary'] = _summary(signals)
    return signals


def _summary(s):
    metrics = '，'.join(part for part in (f"TPS {s['tps']}" if s['tps'] is not None else '',
                                          f"MSPT中位数 {s['mspt_median']} 毫秒" if s['mspt_median'] is not None else '') if part)
    parts = [{'none': f'未见持续过载（{metrics}）',
              'mild': f'轻微过载（{metrics}），多人多维度服务器常见',
              'sustained': f'持续过载（{metrics}）',
              'unknown': '报告缺少 TPS/MSPT，无法判断整体负载'}[s['load']]]
    if s['spikes']:
        parts.append(f"存在偶发尖峰：最慢一次 tick {s['mspt_max']:.0f} 毫秒，约为中位数的 {s['spike_ratio']} 倍")
    elif s['spikes'] is False:
        parts.append('未见明显尖峰')
    wait = s['wait_pct']
    if wait is not None and wait >= 1:
        parts.append({'between_ticks': f'tick 线程约 {wait}% 的时间在等待，MSPT 中位数远低于 {TICK_BUDGET_MS} 毫秒的 tick 预算，'
                                       f'这些主要是 tick 之间的空闲（有余量）',
                      'in_tick': f'tick 线程约 {wait}% 的时间在等待，而 MSPT 中位数已达到 {TICK_BUDGET_MS} 毫秒的 tick 预算，'
                                 f'tick 之间没有空闲，这些等待发生在 tick 内（在等其他线程、区块加载或锁）',
                      'unknown': f'tick 线程约 {wait}% 的时间在等待，无法判断是 tick 之间的空闲还是 tick 内的等待'}[s['wait_kind']])
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


def user_payload(question, overview, comparison=None, previous=None):
    return json.dumps({'user_observation': question[:2000], 'overview': model_overview(overview, question),
                       'history_comparison': comparison,
                       'previous_review_untrusted': previous[:6000] if comparison and previous else None},
                      ensure_ascii=False)

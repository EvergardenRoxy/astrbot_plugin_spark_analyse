"""Deterministic, offline four-panel PNG renderer. Imported only in the render subprocess."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FuncFormatter

BG, CARD, PLOT = '#101720', '#18232f', '#141e29'
TEXT, MUTED, GRID = '#e7edf5', '#9aafc2', '#304253'
BLUE, CYAN, GREEN, AMBER, PURPLE = '#67aaff', '#51d9e7', '#73d39c', '#ffbd65', '#bd9bff'


def render(data, target, timezone='UTC'):
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported chart schema')
    font = FontProperties(fname=str(Path(__file__).with_name('chart_assets')/'SparkChartSans.otf'))
    plt.rcParams.update({'text.usetex': False, 'text.parse_math': False, 'axes.unicode_minus': False})
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        timezone, zone = 'UTC', ZoneInfo('UTC')
    fig = plt.figure(figsize=(16, 10), dpi=150, facecolor=BG)
    def text(x, y, value, size=10, color=TEXT, **kwargs):
        fig.text(x, y, str(value), fontproperties=font, fontsize=size, color=color, **kwargs)
    def card(x, y, w, h):
        fig.add_artist(FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0.008,rounding_size=0.009',
                       transform=fig.transFigure, facecolor=CARD, edgecolor=GRID, linewidth=.7, zorder=0))
    def axes(bounds, ylabel=''):
        ax = fig.add_axes(bounds, facecolor=PLOT)
        ax.tick_params(colors=MUTED, labelsize=8, length=0, pad=4)
        ax.grid(color=GRID, linewidth=.5, alpha=.5)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_ylabel(ylabel, fontproperties=font, color=MUTED, fontsize=8)
        return ax
    def stamp(value):
        return datetime.fromtimestamp(value/1000, zone)
    def time_axes(bounds, ylabel='', bottom=True):
        ax = axes(bounds, ylabel)
        if data['range']:
            lo, hi = data['range']
            ax.set_xlim(stamp(lo), stamp(max(lo+1000, hi)))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=zone))
            ax.tick_params(labelbottom=bottom)
            a, b = data['sampling'].get('start_ms'), data['sampling'].get('end_ms')
            if a and b and b > a and b >= lo and a <= hi:
                ax.axvspan(stamp(max(lo, a)), stamp(min(hi, b)), color=AMBER, alpha=.09)
        else:
            ax.set_xticks([])
        return ax
    def plot(ax, name, color, title, scale=1, style='-', step=False):
        q = data['series'].get(name)
        if not q:
            return False
        points = q['points']
        times = [stamp(p[0]) for p in points]
        values = [p[1]/scale if p[1] is not None else math.nan for p in points]
        if step:
            ax.step(times, values, where='mid', color=color, lw=1.2, label=title)
        else:
            ax.plot(times, values, color=color, lw=1.2, linestyle=style, marker='.', markersize=1.2, label=title)
        return True
    def missing(ax, reason):
        ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)
        ax.text(.5, .5, reason, transform=ax.transAxes, ha='center', va='center',
                fontproperties=font, fontsize=10, color=MUTED)
    def legend(ax, ncol=3):
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            obj = ax.legend(loc='upper left', ncol=ncol, frameon=False, labelcolor=MUTED,
                            handlelength=1.5, columnspacing=.6, prop=font)
            for item in obj.get_texts():
                item.set_fontsize(7.5)
    def metric_scale(ax, names, threshold=100):
        values = [p[1] for name in names for p in data['series'].get(name, {}).get('points', [])
                  if p[1] is not None]
        if values and max(values) > threshold*10:
            ax.set_yscale('symlog', linthresh=threshold)
            # LogFormatter emits mathtext, deliberately disabled for untrusted report labels.
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{value:g}'))
            return True
        return False
    def fmt(value, digits=2):
        return '未提供' if value is None else f'{value:,.{digits}f}'
    try:
        platform = data['platform']
        platform_type = {0:'服务端', 1:'客户端', 2:'代理', 3:'应用'}.get(platform['type'], '未知平台')
        text(.03, .953, 'Minecraft 性能遥测', 23)
        text(.97, .953, f"{platform['brand'] or platform['name']} · {platform_type} · MC {platform['minecraft']} · Java {platform['java']}", 10, ha='right')
        text(.03, .919, f"时区 {timezone} · 目标 {data['sampling']['target_tps']:g} TPS · 曲线为记录点聚合，非逐 tick 事件", 9, MUTED)
        if data['range']:
            text(.03, .887, f"遥测覆盖 {stamp(data['range'][0]):%Y-%m-%d %H:%M:%S}—{stamp(data['range'][1]):%H:%M:%S}；琥珀底色为 profiler 活跃区间", 9, MUTED)
        else:
            text(.03, .887, '无可信时间序列，显示报告快照；不伪造时间曲线', 9, AMBER)
        card(.028, .518, .348, .345); card(.401, .518, .348, .345)
        card(.028, .116, .348, .367); card(.401, .116, .348, .367); card(.775, .116, .197, .747)
        text(.042, .836, '01  Tick time · TPS · 在线人数', 13)
        ax = time_axes([.071, .692, .286, .12], 'Tick / ms', False)
        names = ('tick_max', 'tick_percentile95', 'tick_median')
        present = [plot(ax, key, color, name) for key, color, name in zip(names, (AMBER, PURPLE, BLUE), ('最大值', 'P95', '中位数'))]
        if any(present):
            ax.axhline(data['sampling']['tick_budget_ms'], color='#ed838c', lw=.7, ls='--', label='tick 预算')
            metric_scale(ax, names); ax.set_ylim(bottom=0); legend(ax)
        else:
            missing(ax, '未采集 tick 时间')
        ax = time_axes([.071, .622, .286, .048], 'TPS', False)
        if plot(ax, 'tps', GREEN, 'TPS'):
            ax.set_ylim(bottom=0, top=max(data['sampling']['target_tps']*1.1, ax.get_ylim()[1]))
        else:
            missing(ax, '未采集 TPS')
        ax = time_axes([.071, .553, .286, .045], '人数')
        if not plot(ax, 'players', PURPLE, '人数', step=True):
            missing(ax, '未采集在线人数')

        text(.422, .836, '02  内存与堆详情', 13)
        ax = time_axes([.444, .697, .286, .117], 'GiB', False)
        if 'heap_used' in data['series']:
            same_max = (data['series'].get('heap_committed', {}).get('points') ==
                        data['series'].get('heap_max', {}).get('points'))
            for key, col, name, style in [('used', CYAN, 'Heap used', '-'), ('committed', BLUE, 'Committed = Max' if same_max else 'Committed', '--'), ('max', MUTED, 'Max', ':')]:
                if key == 'max' and same_max:
                    continue
                plot(ax, 'heap_'+key, col, name, 2**30, style)
            ax.set_ylim(bottom=0); legend(ax)
        else:
            usage = data['heap']; capacity = usage['max'] or usage['committed']
            if capacity and usage['used'] is not None:
                ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)
                ax.barh([0], [capacity/2**30], color=GRID, height=.5)
                ax.barh([0], [usage['used']/2**30], color=CYAN, height=.5)
                ax.text(.02, .82, f"结束时快照：{usage['used']/2**30:.2f} / {capacity/2**30:.2f} GiB", transform=ax.transAxes,
                        fontproperties=font, color=TEXT, fontsize=8)
            else:
                missing(ax, '未采集堆内存')
        ax = time_axes([.444, .625, .286, .048], 'MiB')
        if plot(ax, 'nonheap_used', PURPLE, 'Non-heap used', 2**20):
            plot(ax, 'nonheap_committed', MUTED, 'Committed', 2**20, '--')
            legend(ax, 2)
        else:
            value = data['nonheap']['used']
            missing(ax, 'Non-heap 结束快照：'+(f'{value/2**20:.1f} MiB' if value is not None else '未提供'))
        text(.422, .590, '内存池：报告结束时快照（已用 / 已提交）'+(' · 前4项' if len(data['pools']) > 4 else ''), 8.5, MUTED)
        for i, pool in enumerate(data['pools'][:4]):
            y = .566-i*.016
            used, committed = pool['used'], pool['committed']
            text(.422, y, pool['name'][:24], 7.5)
            text(.736, y, f"{fmt(used/2**30 if used is not None else None)} / {fmt(committed/2**30 if committed else None)} GiB", 7.5, MUTED, ha='right')
        if not data['pools']:
            text(.422, .555, '报告未提供内存池详情', 9, MUTED)

        sources = data['sources']
        text(.042, .454, '03  '+sources['title'], 13)
        text(.042, .426, '无 GC 暂停事件时 · 仅 Server thread 保留调用采样', 8.5, AMBER)
        ax = axes([.153, .221, .183, .175])
        rows = sources['rows']
        if rows:
            ys = list(range(len(rows)))
            ax.barh(ys, [r['pct'] for r in rows], height=.55, color=[CYAN, BLUE, PURPLE, GREEN, '#7bbeb8', '#93acda', '#b8a9c8'][:len(rows)])
            ax.set_yticks(ys, [r['name'][:22] for r in rows], fontproperties=font)
            ax.invert_yaxis(); ax.set_xlim(0, max(r['pct'] for r in rows)*1.45)
            for i, row in enumerate(rows):
                ax.text(row['pct']+.015*ax.get_xlim()[1], i, f"{row['pct']:.3f}% / {row['sampled_ms']/1000:.2f}s", color=TEXT, fontsize=7, va='center')
            ax.set_xlabel('主线程采样占比 / %', fontproperties=font, color=MUTED, fontsize=8)
        else:
            missing(ax, sources['reason'])
        denominator = sources.get('denominator_ms')
        text(.042, .164, f"分母：主线程采样 {denominator/1000:,.3f} s（含等待）" if denominator else '无可确认的主线程分母', 8, MUTED)
        text(.042, .140, '不是 CPU 占用；含子调用，来源可重叠，不能求和。', 8, AMBER)

        text(.422, .454, '04  玩家延迟分析', 13)
        ax = time_axes([.444, .221, .286, .175], 'RTT / ms')
        ping_names = ('ping_max', 'ping_percentile95', 'ping_median')
        found = [plot(ax, key, col, name, style=ls) for key, col, name, ls in zip(ping_names, (MUTED, PURPLE, BLUE), ('最大值', 'P95', '中位数'), ('--', '-', '-'))]
        if any(found):
            metric_scale(ax, ping_names); ax.set_ylim(bottom=0); legend(ax)
        else:
            values = [(name, data['ping'][key]) for name, key in [('最小值', 'min'), ('中位数', 'median'), ('平均值', 'mean'), ('P95', 'percentile95'), ('最大值', 'max')] if data['ping'][key] is not None]
            if values:
                ax.set_xticks([]); ax.clear(); ax.set_facecolor(PLOT)
                ys = list(range(len(values)))
                ax.barh(ys, [v for _, v in values], color=BLUE, height=.4)
                ax.set_yticks(ys, [name for name, _ in values], fontproperties=font); ax.invert_yaxis()
                ax.tick_params(colors=MUTED, labelsize=8); ax.set_xlabel('最近 15 分钟快照 / ms', fontproperties=font, color=MUTED, fontsize=8)
            else:
                missing(ax, '未采集玩家延迟')
        text(.422, .164, '玩家 Ping 聚合；快照不伪造成曲线，不能推算丢包。', 8, MUTED)
        text(.422, .140, '大尖峰使用 symlog（0–100 ms 线性，其上对数刻度）。', 8, MUTED)

        text(.783, .836, 'GC 摘要', 17)
        text(.783, .807, '收集耗时不等于 STW 暂停时长', 9, AMBER)
        y = .762
        for kind, title in [('platform', '平台统计（起止未核实）'), ('system', 'JVM 全程收集统计')]:
            text(.783, y, title, 10, CYAN); y -= .034
            rows = data['gc'][kind]
            if not rows:
                text(.783, y, '报告未提供', 9, MUTED); y -= .052
            for row in rows[:4]:
                if y < .23:
                    break
                text(.783, y, row['name'][:27], 8.5); y -= .026
                count = '未提供' if row['count'] is None else str(row['count'])
                text(.783, y, f"次数 {count} · 平均 {fmt(row['avg_ms'])} ms", 8, MUTED); y -= .022
                text(.783, y, '平均间隔 '+fmt(row['interval_ms']/1000 if row['interval_ms'] is not None else None)+' s', 8, MUTED); y -= .036
        text(.783, .185, '无事件时间戳，不虚构单次暂停', 8.5, AMBER)
        text(.783, .148, '平台与 JVM 全程统计不相加', 8.5, MUTED)
        a, b = data['sampling'].get('start_ms'), data['sampling'].get('end_ms')
        sample = f'{stamp(a):%H:%M:%S}—{stamp(b):%H:%M:%S}' if a and b and b > a else '起止未知'
        text(.03, .075, f'调用采样区间 {sample}；来源榜仅对应保留调用窗口，不能冒充全部遥测期间。', 9, MUTED)
        warning = '；'.join(data['warnings'][:2]) or '原始采样占比不等于根因；内存池为结束快照，缺失数据不按零值绘制。'
        text(.03, .043, warning[:110], 8.5, AMBER if data['warnings'] else MUTED)
        fig.savefig(target, facecolor=BG, format='png')
    finally:
        plt.close(fig)

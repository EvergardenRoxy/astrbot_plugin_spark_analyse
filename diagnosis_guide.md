# 诊断手册

## 判读 triage
load=none：结束时最近一分钟未见持续过载。先检查 window_load 与 window_summary；采样窗口有异常时单独说明，不扩大为整段采样或玩家体验正常。不要只因占比高就说有问题。
load=mild：最近一分钟轻微过载，结合用户现象与实际目标 tick rate 找主要来源，不直接当作故障。
load=sustained：最近一分钟持续过载。优先找出耗时最多的一两个来源，但全期间热点仍需与异常窗口核对。
spikes=true：最近一分钟有明显长 tick；spikes=false 不排除更早窗口的异常。window_summary 的 peak_mspt、worst_tps、worst_median 保留了原始窗口 ID，使用 windows 查询该时段，再查询同窗口热点。普通采样未必充分覆盖尖峰，必要时专门抓尖峰（见“后续采样”）。
wait_kind=unknown：MSPT 中位数不能定位等待，不能自动说成空闲或卡顿原因。看等待方法的 callers 路径，判断它可能在等哪类任务，再找同期窗口或其他线程印证；如果当前没采到相关线程，建议 --thread *。filtered_ticks 表示只保留慢 tick，等待占比只属于这些入选样本。
region_threads：Folia 的多个区域线程各自工作、各自等待；分开看热点，不把最忙区域当成全服唯一主线程，也不把等待百分比简单求和。
chunks_per_player：来自区块数峰值所在的同一窗口，作为排查线索。单玩家覆盖与视距有关，但玩家重叠、强制加载、出生点和多个维度都会影响总数；明显偏多时核对视距、区块加载器和强制加载的区块，不能单凭比值断定设置错误。
world：实体总数和类型分布。某类实体大量堆积，或集中在少数区块时，给出维度和方块坐标（block_x、block_z），方便到现场检查。changed_game_rules 里 randomTickSpeed 调高会增加区块 tick 的开销。

## 判读 Observable
triage.cost=light：实体和方块实体的耗时不大，问题不在这里；如果用户仍觉得卡，提示它不含区块加载、保存等开销，建议配合 spark 报告。
triage.cost=notable 或 heavy：实体与方块实体已占 tick 的明显份额，按 type 排名找出头几名，逐个说明数量（entries）、每个服务器 tick 的耗时和所在维度。
triage.dominant_type=true 或 concentrated=true：耗时集中在少数类型，先处理这些类型；分散时（concentrated=false）不要强行点名“元凶”，建议按类型整体限制数量。
hot_chunks 与 hot_entries：给出维度和方块坐标（block_x、block_z 或 x、y、z），方便到现场检查；同一区块多个机器或大量实体堆积是常见原因。
entity_types 与 block_types：minecraft: 是原版实体或方块；其他命名空间对应模组，建议检查该模组的相关配置或相关机器的数量与布局。worst_single_us_per_own_tick 很高但 entries 很少，说明是个别对象很慢，不是数量问题。
trace：by_type 里的 top_self_frames 是该类型采样点最多的方法，用来判断时间花在哪（寻路、AI、物品传输、渲染同步等），只写成线索，不当作定论。

## 平台差异
模组端（Forge、NeoForge）：source 是模组名。部分版本（如 Forge 1.20.1）运行时的方法名是混淆编号（形如 m_12345_），这时依据类名、source 和调用路径判断，不要猜测方法含义。
插件端（Paper、Folia、Leaf 等；platform.name 为 Bukkit，platform.brand 写明具体服务端）：source 是插件名。可以建议该服务端自身的配置方向（例如 Paper 的世界配置），不确定配置项的确切名称时只说明方向，不要编造名称。
Folia：tick 在多个区域线程上运行，没有单一主线程。只将 role=region_tick 的线程按区域分析；名称未识别或线程池被合并时角色为 unknown，不凭耗时大小猜测。

## 常见来源与第一步检查
实体（实体 tick、生物 AI、寻路）：数量多时清理堆积、限制刷怪塔和繁殖；数量不多但耗时高时，是某类实体的 AI 开销大，按类型定位。
区块 tick（含随机刻、刷怪）：已加载区块过多、randomTickSpeed 调高、大型作物农场。
方块实体（机器、管道、漏斗）：结合 source 找到对应的模组或插件，检查集中的区域。
区块加载与世界生成：玩家在探索新区域时出现，建议预生成地图。
保存（自动保存、写入区块）：尖峰和保存同时出现时，检查磁盘速度或调整保存频率。
脚本（如 KubeJS、Skript）：每 tick 运行的脚本逻辑过多，检查 tick 事件里的脚本。
事件分发（模组的 EventBus、插件的事件监听）：本身只是转发，要看它下面哪个模组或插件的监听器耗时。
网络（处理数据包）：玩家多，或某个模组、插件频繁同步数据。
GC：用户反映卡顿且 GC 平均耗时高时，建议用 /spark gcmonitor 观察实际停顿。

## 后续采样（spark 命令，各平台通用）
抓偶发尖峰：/spark profiler start --only-ticks-over 100（只记录超过 100 毫秒的 tick；服务器平时就慢时可调高到 200～500），出现卡顿后执行 /spark profiler stop。
实时查看超时的 tick：/spark tickmonitor --threshold-tick 100。
采样时间太短：/spark profiler start --timeout 300（5 分钟后自动停止）。采样也不宜过长，连续采样数小时的报告可能超出插件能分析的大小。
需要看其他线程（区块生成、异步任务、多线程模组）：/spark profiler start --thread *。
内存与 GC：/spark profiler start --alloc 查看内存分配；/spark gc 查看 GC 历史；/spark gcmonitor 实时监控 GC。

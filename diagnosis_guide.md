# 诊断手册

## 判读 triage
load=none：服务器整体不卡。不要把占比较高的部分说成“问题”；如果 spikes=true，重点讲尖峰。
load=mild：轻微过载。多人、多维度的服务器很常见（原版在一个线程上依次计算所有维度），一般不需要紧急处理；用户确实感到卡顿时再找主要来源。
load=sustained：持续过载。优先找出耗时最多的一两个来源。
spikes=true：存在偶发的长 tick。平均值正常不代表不卡，普通采样常常抓不到尖峰，应建议专门抓尖峰（见“后续采样”）。常见诱因：自动保存、生成新区块、玩家登录、大量实体同时加载、GC 停顿。
wait_kind=between_ticks：等待主要是 tick 之间的空闲，说明有余量，不是问题。
wait_kind=in_tick：tick 已超出预算，等待发生在 tick 内。看等待热点（如 Unsafe.park）的调用路径，找出它在等什么：多线程或异步的模组/插件、区块加载、存档、锁。如果是在等其他线程，真正的耗时不在这份报告里，建议用 --thread * 重新采样。
wait_kind=unknown：无法判断等待的性质，不要把等待说成空闲，也不要说成卡顿原因。
chunks_per_player：每名玩家约加载 (2×视距+1)² 个区块，视距 10 时约 441 个。明显偏多时，检查视距设置、区块加载器和强制加载的区块。
world：实体总数和类型分布。某类实体大量堆积，或集中在少数区块时，给出维度和方块坐标（block_x、block_z），方便到现场检查。changed_game_rules 里 randomTickSpeed 调高会增加区块 tick 的开销。

## 平台差异
模组端（Forge、NeoForge）：source 是模组名。部分版本（如 Forge 1.20.1）运行时的方法名是混淆编号（形如 m_12345_），这时依据类名、source 和调用路径判断，不要猜测方法含义。
插件端（Paper、Folia、Leaf 等；platform.name 为 Bukkit，platform.brand 写明具体服务端）：source 是插件名。可以建议该服务端自身的配置方向（例如 Paper 的世界配置），不确定配置项的确切名称时只说明方向，不要编造名称。
Folia：tick 在多个区域线程上运行，没有单一主线程，报告里的 tick 线程是区域调度线程。

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

import json
from pathlib import Path
p = Path('_conf_schema.json')
s = json.loads(p.read_text(encoding='utf-8'))
texts = {
'analysis_provider_id': ('用于分析报告的模型', '先在AstrBot中添加模型，再在这里选择。请选择支持工具调用的模型；未选择时不会开始分析。'),
'fallback_providers': ('备用分析模型', '首选模型出错时，按列表从上到下尝试。留空表示不使用备用模型。'),
'acknowledgement_text': ('收到报告后的回复', '开始分析前只发送一次。可以写成你喜欢的文字，留空则不回复，最多1000个字符。'),
'download_timeout_seconds': ('下载等待时间（秒）', '下载报告最多等待多久。默认120秒，网络较慢时可以适当增加。'),
'parse_timeout_seconds': ('解析等待时间（秒）', '读取报告并整理分析数据最多等待多久。默认90秒，大报告可以适当增加。'),
'auto_analyze': ('自动分析报告链接', '开启后，聊天中同时出现分析请求和Spark链接就会开始分析；关闭后可使用/spark命令或人格工具。'),
'access_mode': ('谁可以使用', 'admin_only：仅AstrBot管理员；admin_and_whitelist：管理员和下方用户名单；all：所有人。这里的管理员是AstrBot管理员，不是QQ群管理员。'),
'user_whitelist': ('允许使用的用户', '每项填写一个用户ID，例如QQ号。仅在“管理员和用户名单”模式下生效。'),
'allowed_origins': ('允许使用的聊天', '留空允许所有聊天。需要限制时填写AstrBot的会话标识（UMO），不是只填QQ群号；人员权限仍需满足。'),
'history_enabled': ('保存分析历史', '开启后保存分析结果供以后比较，保留30天。关闭不会删除已有历史，也不影响报告缓存。'),
'analysis_prompt': ('分析回复要求', '可以修改分析重点、语气和篇幅。报告数据会自动提供，不用填写占位符。留空恢复默认；保存后重载插件。'),
'profile_cache_hours': ('报告缓存时间（小时）', '默认72小时，从最近一次使用开始计时。同一报告再次使用会重新计时，即使模型分析失败也保留已下载的报告。')}
assert set(s) == set(texts)
for key, (description, hint) in texts.items():
    s[key]['description'], s[key]['hint'] = description, hint
provider = s['fallback_providers']['templates']['provider']
provider['name'] = '添加备用模型'
provider['items']['provider_id']['description'] = '选择备用模型'
provider['items']['provider_id']['hint'] = '选择已经在AstrBot中配置好的模型，出错时按顺序使用。'
p.write_text(json.dumps(s, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

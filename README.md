# 插件二级缓存

![logo](logo.png)

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3Dv4.0.0-2ea44f)](https://github.com/Soulter/AstrBot)
![GitHub stars](https://img.shields.io/github/stars/Fangnai-byte/astrbot_plugin_plugin_cache)

不常用的插件常驻，会一直占用 LLM 的工具列表和系统提示词。这个插件把它们收进"二级缓存"：平时保持关闭，需要时唤醒，用完关掉。

## 工作方式

1. 在配置里登记"受管插件"：
   - **受管插件**：WebUI 直接点选（列表来自当前已启用的插件，支持搜索、全选；填 `*` 表示除本插件外的全部插件）。
   - **补充登记**：休眠中的插件选不到，或想给某个插件补上描述与关键词时，在这一项里手填 `插件名|一句话描述|关键词1,关键词2`，一行一个。
2. 平时这些插件处于休眠（未加载），不占工具位、不进提示词。
3. 休眠清单会被注入系统提示词的末尾，模型知道有哪些插件可以按需唤醒。
4. 模型需要时调用 `plugin_cache_load` 唤醒；用完调用 `plugin_cache_release`。
5. 用户在消息里说到关键词，也会自动预热对应的受管插件。
6. 超过 `idle_minutes` 没用过，看门狗自动让它休眠。

## 指令

- `/插件缓存`：查看每个受管插件的运行状态。

## 注意

- 唤醒发生在本次请求的工具列表已经确定之后，所以插件本体要**下一轮**才真正可用。模型会先给用户一句"稍等一下"，下一轮再调用它的工具。
- 走的还是 AstrBot 自带的热重载开关，关闭时会把 handler 置为未激活并落盘，重启后仍是休眠状态。
- 不要在受管列表里写自己（`astrbot_plugin_plugin_cache`）。

## License

MIT

# 结构决策

行为不变量见 `BEHAVIOR_CONTRACT.md`。本页只记结构取舍。

## 双面板

`CONFIG_SPECS.surfaces` 区分官方 Dashboard（`host`）与自定义设置页（`panel`）。常用键上自定义页；巡检、勿扰、回复长度、日上限、`log_reply_content` 等只在 Dashboard。两套面板读写同一份配置。前端可写键必须等于 panel 面，由 `test_fe_writable_keys_match_panel_surfaces` 锁定。

## 装配模块

协作对象接线留在 `assembly.py`。循环依赖与宿主热替换用调用期查找。容器持有者守卫同时扫描 `main.py` 与 `assembly.py`。

## 设置页 chrome

浅/深/跟随系统主题经 `GET/POST ui/theme` 写入 `ui_prefs.json`（页面在 Dashboard iframe 内，localStorage 不可靠）。压暗与粗体是页面能力，浏览器测试覆盖。主题落盘与状态文件共用 `storage` 原子写。

## 默认值

运行默认以 `CONFIG_SPECS` 为准。README 建议区间不是默认值；文档写建议时同时写默认。

## 测试与门禁

覆盖率门槛（`fail_under` 与 `scripts/coverage_gates.py`）只上调。不为减行数删除行为测试或下调门槛。

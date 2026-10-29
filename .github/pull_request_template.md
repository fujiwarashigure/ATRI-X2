<!--
提交前请逐条勾选。前两条对应"文档从软副本变成编译产物"的闭环，
第三条对应"改行为必须带测试"。
-->

- [ ] **动了模块结构**（新增/删除/改名/移动 `atri_x/**`）→ 跑过
      `python scripts/gen_index.py` 并提交了 `devdocs/generated/` 的改动
- [ ] **改了行为** → 更新了对应测试（`tests/test_*.py`），或在下方说明为何不适用
- [ ] **改了命令 / 配置键 / 工具** → 更新了 `docs/commands.md` /
      `docs/configuration.md` / `docs/dev/find-it.md`（`python scripts/check_docs.py` 会验）

## 变更说明

<!-- 一两句话说清"从旧状态变成新状态"；补丁式写法只用于 CHANGELOG。 -->

## 验证

<!-- 贴关键门禁输出：check_syntax / check_imports / check_docs / 相关单测 -->

```
python scripts/check_syntax.py
python scripts/check_imports.py
python scripts/check_integration.py
python scripts/check_secrets.py
python scripts/check_except.py
python scripts/gen_index.py --check
python scripts/check_docs.py
python -m unittest discover -s tests -t .
```

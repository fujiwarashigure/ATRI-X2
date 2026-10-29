# research_pack：随包研究引擎与技能（1.1.3 起）

这是**科研研究助理**的随包数据目录（不是 Python 包）。发行包里的位置：
`<安装目录>/_internal/research_pack/`（便携版同样是包内 `_internal/`）。

## 目录

| 路径 | 说明 |
| --- | --- |
| `pack.json` | 引擎版本、文件清单与内容摘要（`digest`），首启安装的幂等/升级依据；由 `tools/gen_pack.py` 生成 |
| `settings.json` | 引擎默认设置（检索源、速率、核验阈值、评审权重、导出默认值）；首次安装时落盘，用户改过的不覆盖 |
| `engine/` | 研究引擎源码：`run.py` + `atrix_research/`（38 个模块，约 8.7k 行，纯标准库） |
| `skills/<skill>/SKILL.md` | 15 个 AgentSkill 文档；12 个硬工具的文档带**由引擎生成的「工具契约」附录** |
| `skills/<skill>/manifest.json` | 12 个硬工具的清单数据（参数 JSON Schema、`[args]` 映射、风险、超时），首启时渲染成 `ATRIX_SKILL.toml` |

## 运行时怎么用

1. 首次启动（冻结产物）时，`atri_x/research/bootstrap.py` 把 `engine/` 复制到
   `%APPDATA%\ATRI-X\research\engine`、把 `skills/*` 装进 `%APPDATA%\ATRI-X\skills\agent\`，
   并按运行形态生成 `ATRIX_SKILL.toml`：
   - 冻结版：`["<atri-x.exe>", "-m", "atri_x.research.runner", "--home", "<研究目录>", "<tool>"]`
   - 源码树（显式 `--source`）：`["<python>", "<研究目录>/engine/run.py", "--home", "<研究目录>", "<tool>"]`
2. 装完写 `%APPDATA%\ATRI-X\research\install.json`（`schema=atrix-research/install/2`，`mode=bundled`）。
3. 升级发行包后，`pack.json` 的 `digest` 变化 → 下次启动自动重装引擎与技能；`settings.json` 与
   `runs/` 不覆盖。

引擎**不进 PyInstaller PYZ**（它是数据文件）：换引擎不需要重打包，也不会出现"PYZ 里一份旧引擎
把配置目录的新引擎盖掉"。可选依赖 `reportlab` / `pypdf`（PDF 导出与全文抽取）由打包时收进 PYZ。

## 要不要手工改这里

一般不要。这里的内容与交付的 `atri-research` 包同源，改动流程：

```text
# 1) 在 atri-research/ 的引擎里改（含 capabilities.py 的硬工具规格）
# 2) 刷新技能文档附录
python make_skills.py --write
python make_skills.py --check
# 3) 重新生成 research_pack/（复制 engine+skills、写 manifest.json / settings.json / pack.json）
python <仓库工具>/gen_pack.py <本仓库>/research_pack
# 4) 跑门禁与打包核验
python scripts/check_syntax.py && python -m unittest tests.test_research_bootstrap
```

`research_pack/` 被误删时 `packaging/atri-x.spec` 会**直接让构建失败**（不会静默出一个没有研究
助理的包）；`packaging/build.py` 的产物核验会跑 `-m atri_x.research.bootstrap --check` 与
`-m atri_x.research.runner … version`，确保冻结产物真能装、真能跑。

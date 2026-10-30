# 构建材料（不入库）

本目录放**体积大、不属于源码**的构建输入，`.gitignore` 只忽略 `drawio/` 子目录，README 本身随仓库提交。

## drawio/

内置 draw.io 渲染器（1.1.5 起图表能力必需）：

```text
packaging/vendor/drawio/          ← draw.io 32.6.1 Windows 解包目录（官方 zip 解压后直接放这里）
  draw.io.exe
  resources/app.asar
  *.dll / *.pak / *.dat / locales/
```

- 构建：`python packaging/build.py --with-drawio D:\draw.io-32.6.1-windows --release`
  （不指定 `--with-drawio` 时默认读本目录；缺 `draw.io.exe` 会让构建**直接失败**）。
- 用法与体积见 [packaging/README.md](../README.md) §3.7；运行时只读，不复制到用户配置目录。
- 裁剪：删掉 `locales/` 下除 `en-US.pak` / `zh-CN.pak` 之外的语言包可省约 49 MB；
  **`ffmpeg.dll` 不能删**（缺了渲染进程直接崩）。

许可证：draw.io 桌面版为 Apache-2.0（JGraph），内含 Electron / Chromium 及其第三方组件，
许可证文本随解包目录一起分发；详见 [LEGAL.md](../../LEGAL.md) §六。

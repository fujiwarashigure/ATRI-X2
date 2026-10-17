"""测试包。

存在的理由：``python -m unittest discover -s tests -t .`` 要求 start 目录是可
导入的包，否则报 ``ImportError: Start directory is not importable``。
（另一种等价写法是 ``-t tests``，但仓库统一用 ``-t .``，这样 ``tests`` 与
``atri_x`` 同处顶层，导入路径更好推理。）
"""

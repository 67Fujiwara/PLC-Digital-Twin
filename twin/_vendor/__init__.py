"""同梱ライブラリ。pip が使えない PC でも動くように、PyYAML (純 Python 部分) を入れてある。
`import yaml` が失敗したときだけ twin/__init__.py がここを sys.path に足す。"""

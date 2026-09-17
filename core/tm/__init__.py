"""术语表（Glossary）子系统 —— 翻译记忆（TM）的最小可用切面。

当前阶段只落地「静态术语表 + 确定性最长匹配」这一层：不依赖数据库、
不依赖网络、可复现。后续接入 SQLite WAL 记忆库或向量召回时，
只需替换 :func:`match_glossary` 的实现与 :data:`GLOSSARY` 的来源，
RPC 面（``match_glossary``）与壳层消费方式保持不变。

公开 API：``GlossaryEntry``、``GlossaryMatch``、``GLOSSARY``、``match_glossary``。
"""

from .glossary import GLOSSARY, GlossaryEntry, GlossaryMatch, match_glossary

__all__ = ["GLOSSARY", "GlossaryEntry", "GlossaryMatch", "match_glossary"]

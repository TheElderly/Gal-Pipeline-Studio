"""Engine Profiler 实现 —— 目录特征 + PE 特征字符串的双层指纹嗅探。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from core.adapters.base import EngineAdapterError

# ---------------------------------------------------------------------------
# 常量与扫描护栏（护栏是纪律：侦察绝不能反过来拖垮流水线）
# ---------------------------------------------------------------------------

_PE_HEADER_BYTES = 64 * 1024
"""每个可执行文件参与特征串扫描的头部字节数（引擎特征串几乎都在此区间）。"""

_MAX_EXE_HEADERS = 8
"""最多读取的可执行文件数（按文件名排序取前 N，保证确定性）。"""

_SUBDIR_SCAN_DEPTH = 1
"""子目录 glob 扫描深度（只看一层：plugin/ 等特征目录都在浅层）。"""


class ProfileEngineError(EngineAdapterError):
    """引擎判定失败：目录不存在等（侦察输入违约，非引擎特征缺失）。"""


@dataclass(frozen=True)
class _EvidenceRule:
    """单条指纹证据规则：命中即按 weight 计分，描述写入 Profile 供审计。"""

    engine_id: str
    weight: float
    description: str
    glob: str | None = None
    subdir: str | None = None
    pe_signature: bytes | None = None


# 指纹规则库（新增引擎 = 追加规则，不动判定逻辑）。
# 权重设计：特征封包 > PE 特征串 > 特征目录 —— 封包是引擎身份的硬证据。
_RULES: tuple[_EvidenceRule, ...] = (
    # --- KiriKiri / KAG（吉里吉里）---
    _EvidenceRule("kirikiri", 0.6, "存在特征封包 data.xp3", glob="data.xp3"),
    _EvidenceRule("kirikiri", 0.4, "存在 *.xp3 封包", glob="*.xp3"),
    _EvidenceRule("kirikiri", 0.2, "存在特征子目录 plugin/", subdir="plugin"),
    _EvidenceRule("kirikiri", 0.4, "可执行文件含特征串 KiriKiri", pe_signature=b"KiriKiri"),
    # --- BGI / Ethornell（BGI 引擎）---
    _EvidenceRule("bgi", 0.5, "存在 BGI 系可执行文件 bgi*.exe", glob="bgi*.exe"),
    _EvidenceRule("bgi", 0.4, "存在 *.arc 封包（BGI/Ethornell 常用）", glob="*.arc"),
    _EvidenceRule("bgi", 0.6, "可执行文件含特征串 Ethornell", pe_signature=b"Ethornell"),
    _EvidenceRule("bgi", 0.3, "可执行文件含特征串 buriko", pe_signature=b"buriko"),
    # --- Majiro ---
    _EvidenceRule("majiro", 0.5, "存在 *.mj 脚本（Majiro 中间码）", glob="*.mj"),
    _EvidenceRule("majiro", 0.4, "存在 *.mjil 封包", glob="*.mjil"),
    _EvidenceRule("majiro", 0.6, "可执行文件含特征串 Majiro", pe_signature=b"Majiro"),
    # --- CatSystem2（含 CS2）---
    _EvidenceRule("catsystem2", 0.5, "存在 *.int 封包（CatSystem2）", glob="*.int"),
    _EvidenceRule("catsystem2", 0.6, "可执行文件含特征串 CatSystem", pe_signature=b"CatSystem"),
    _EvidenceRule("catsystem2", 0.3, "可执行文件含特征串 CS2", pe_signature=b"CS2"),
)

_RECOMMENDED_UNPACKERS: dict[str, str] = {
    "kirikiri": "GARbro CLI（跨进程，GPL 物理隔离）/ FreeMote（PSB/E-mote 附属）",
    "bgi": "YuriSizuku / GARbro CLI（跨进程，GPL 物理隔离）",
    "majiro": "MajiroTool（跨进程 CLI）",
    "catsystem2": "GARbro CLI（跨进程，GPL 物理隔离）",
}

_ENGINE_DISPLAY: dict[str, str] = {
    "kirikiri": "KiriKiri / KAG（吉里吉里）",
    "bgi": "BGI / Ethornell",
    "majiro": "Majiro",
    "catsystem2": "CatSystem2 (CS2)",
}


# ---------------------------------------------------------------------------
# Profile 模型（pydantic：可直接进 project.json 与 RPC 回包）
# ---------------------------------------------------------------------------


class EngineEvidence(BaseModel):
    """一条命中证据（rule 描述 + 得分），判定过程可审计。"""

    engine_id: str
    weight: float = Field(ge=0.0)
    description: str


class EngineProfile(BaseModel):
    """引擎判定画像：模块 0 的唯一交付物。"""

    game_dir: str
    detected: bool
    engine_type: str | None = None
    engine_display: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[EngineEvidence] = Field(default_factory=list)
    recommended_unpacker: str | None = None
    scanned_exes: int = Field(default=0, description="参与 PE 特征扫描的可执行文件数")


# ---------------------------------------------------------------------------
# 侦察实现
# ---------------------------------------------------------------------------


def _iter_evidence_files(game_dir: Path) -> list[Path]:
    """收集候选文件清单：根目录一层 + 子目录一层（浅层特征足够，深扫是大盘成本）。"""
    seen: set[Path] = set()
    files: list[Path] = []
    for item in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
        if item.is_file():
            files.append(item)
            seen.add(item)
        elif item.is_dir() and _SUBDIR_SCAN_DEPTH >= 1:
            for sub in sorted(item.iterdir(), key=lambda p: p.name.lower()):
                if sub.is_file() and sub not in seen:
                    files.append(sub)
                    seen.add(sub)
    return files


def _match_glob(name: str, pattern: str) -> bool:
    """大小写不敏感的 fnmatch（玩家盘上的封包名大小写不可信）。"""
    from fnmatch import fnmatch

    return fnmatch(name.lower(), pattern.lower())


def _exe_pe_signatures(game_dir: Path) -> tuple[dict[bytes, bool], int]:
    """读取根目录可执行文件头部字节，返回 {特征串: 是否命中} 与实扫数量。"""
    exes = sorted(
        (p for p in game_dir.iterdir() if p.is_file() and p.suffix.lower() == ".exe"),
        key=lambda p: p.name.lower(),
    )[:_MAX_EXE_HEADERS]
    signatures = {rule.pe_signature for rule in _RULES if rule.pe_signature}
    hits: dict[bytes, bool] = {}
    for exe in exes:
        try:
            head = exe.open("rb").read(_PE_HEADER_BYTES)
        except OSError:
            continue
        for sig in signatures:
            if sig not in hits and sig in head:
                hits[sig] = True
    return hits, len(exes)


def profile_engine(game_dir: Path) -> EngineProfile:
    """对游戏根目录做只读指纹侦察，产出结构化画像。

    判定纪律：置信度 = 命中证据权重和夹取到 [0,1]；多引擎并存取最高分，
    平分按规则注册序（先注册者优先），结果确定可复现。
    """
    if not game_dir.is_dir():
        raise ProfileEngineError(f"游戏目录不存在或不可访问：{game_dir}")

    files = _iter_evidence_files(game_dir)
    pe_hits, exe_count = _exe_pe_signatures(game_dir)

    scores: dict[str, float] = {}
    evidence: list[EngineEvidence] = []
    for rule in _RULES:
        hit = False
        if rule.glob is not None:
            hit = any(_match_glob(f.name, rule.glob) for f in files)
        elif rule.subdir is not None:
            hit = (game_dir / rule.subdir).is_dir()
        elif rule.pe_signature is not None:
            hit = bool(pe_hits.get(rule.pe_signature))
        if hit:
            scores[rule.engine_id] = scores.get(rule.engine_id, 0.0) + rule.weight
            evidence.append(
                EngineEvidence(
                    engine_id=rule.engine_id,
                    weight=rule.weight,
                    description=rule.description,
                )
            )

    if not scores:
        return EngineProfile(
            game_dir=str(game_dir), detected=False, scanned_exes=exe_count
        )

    # 最高分胜出；平分按规则注册序 —— 取 evidence 里首个达到最高分的引擎
    best_score = max(scores.values())
    best_engine = next(
        e.engine_id for e in evidence if scores[e.engine_id] == best_score
    )
    return EngineProfile(
        game_dir=str(game_dir),
        detected=True,
        engine_type=best_engine,
        engine_display=_ENGINE_DISPLAY.get(best_engine, best_engine),
        confidence=min(1.0, best_score),
        evidence=evidence,
        recommended_unpacker=_RECOMMENDED_UNPACKERS.get(best_engine),
        scanned_exes=exe_count,
    )

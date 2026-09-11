"""静态分析：AST 索引与接口 Diff（实施设计第 13 节）。

Phase 0 只用确定性工具：Python `ast` 做直接定义提取（置信度 1.0）。
`unresolved_references` 目前只记录"动态导入/无法静态解析"的情况；基于
ripgrep 的文本调用关系推断（confidence <= 0.6）留给 `search_code` Tool
（Milestone 6）使用，不在这里重复实现——两者服务的目标不同：这里是给
下游 WorkItem 生成"接口变化了什么"的确定性 Diff，不是给 Agent 做模糊
代码检索。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ExportedSymbol


class SymbolRecord(StrictModel):
    qualified_name: str
    file_path: str
    line: int
    signature: str
    imports: tuple[str, ...]
    confidence: float
    unresolved_references: tuple[str, ...]


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 - 极端语法结构下退化为空参数签名，不阻断整体扫描
        args = "..."
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({args}){returns}"


def _module_imports(tree: ast.Module) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(dict.fromkeys(names))  # 去重且保持顺序


def parse_symbols_from_source(source: str, relative_path: str) -> tuple[SymbolRecord, ...]:
    """解析单个文件的源码文本，提取模块级/类级函数、类和常量定义。

    供 `build_symbol_index`（扫描整个仓库）和 `integrate_patch`（只需要
    对比 patch 涉及的少数文件在补丁前后的符号差异）共用，避免为了对比
    几个文件而重新走一遍整棵目录树。
    """
    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError:
        return (
            SymbolRecord(
                qualified_name=relative_path,
                file_path=relative_path,
                line=0,
                signature="<parse_error>",
                imports=(),
                confidence=0.0,
                unresolved_references=(relative_path,),
            ),
        )

    imports = _module_imports(tree)
    records: list[SymbolRecord] = []
    for node in tree.body:
        records.extend(_extract_top_level(node, relative_path, imports))
    return tuple(records)


def build_symbol_index(root: Path) -> tuple[SymbolRecord, ...]:
    """扫描 `root` 下所有 `.py` 文件，提取模块级/类级函数、类和常量定义。"""
    records: list[SymbolRecord] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            records.append(
                SymbolRecord(
                    qualified_name=relative,
                    file_path=relative,
                    line=0,
                    signature="<parse_error>",
                    imports=(),
                    confidence=0.0,
                    unresolved_references=(relative,),
                )
            )
            continue
        records.extend(parse_symbols_from_source(source, relative))
    return tuple(records)


def _extract_top_level(
    node: ast.stmt, relative_path: str, imports: tuple[str, ...]
) -> list[SymbolRecord]:
    records: list[SymbolRecord] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        records.append(
            SymbolRecord(
                qualified_name=node.name,
                file_path=relative_path,
                line=node.lineno,
                signature=_format_signature(node),
                imports=imports,
                confidence=1.0,
                unresolved_references=(),
            )
        )
    elif isinstance(node, ast.ClassDef):
        records.append(
            SymbolRecord(
                qualified_name=node.name,
                file_path=relative_path,
                line=node.lineno,
                signature=f"class {node.name}",
                imports=imports,
                confidence=1.0,
                unresolved_references=(),
            )
        )
        for member in node.body:
            if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                records.append(
                    SymbolRecord(
                        qualified_name=f"{node.name}.{member.name}",
                        file_path=relative_path,
                        line=member.lineno,
                        signature=_format_signature(member),
                        imports=imports,
                        confidence=1.0,
                        unresolved_references=(),
                    )
                )
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                records.append(
                    SymbolRecord(
                        qualified_name=target.id,
                        file_path=relative_path,
                        line=node.lineno,
                        signature=f"{target.id} = ...",
                        imports=imports,
                        confidence=1.0,
                        unresolved_references=(),
                    )
                )
    return records


def diff_exported_symbols(
    before: tuple[SymbolRecord, ...], after: tuple[SymbolRecord, ...]
) -> tuple[ExportedSymbol, ...]:
    """比较补丁前后的符号集合，得到下游需要知道的接口变化。

    只比较"名字是否存在"和"签名是否变化"，不做语义等价判断——
    Phase 0 的目标是给下游一个确定性、可解释的变化列表，不是完美的
    diff 算法。
    """
    before_by_name = {record.qualified_name: record for record in before}
    after_by_name = {record.qualified_name: record for record in after}

    symbols: list[ExportedSymbol] = []
    for name, after_record in after_by_name.items():
        kind = _guess_kind(after_record.signature)
        if name not in before_by_name:
            change: Literal["added", "modified", "removed", "unchanged"] = "added"
        elif before_by_name[name].signature != after_record.signature:
            change = "modified"
        else:
            change = "unchanged"
        symbols.append(
            ExportedSymbol(
                qualified_name=name,
                kind=kind,
                signature=after_record.signature,
                change=change,
                confidence=after_record.confidence,
            )
        )

    for name, before_record in before_by_name.items():
        if name not in after_by_name:
            symbols.append(
                ExportedSymbol(
                    qualified_name=name,
                    kind=_guess_kind(before_record.signature),
                    signature=before_record.signature,
                    change="removed",
                    confidence=before_record.confidence,
                )
            )

    return tuple(symbols)


def _guess_kind(signature: str) -> Literal["function", "class", "constant"]:
    if signature.startswith("class "):
        return "class"
    if signature.startswith("def ") or signature.startswith("async def "):
        return "function"
    return "constant"

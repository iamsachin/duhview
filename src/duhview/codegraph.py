"""Code structure for the agent: where a symbol is defined, where it is used, and file outlines.

Nothing is indexed up front. A query finds the files that mention the symbol with `git grep`
(fast even on very large repos) and parses only those with tree-sitter, so cost grows with how
often a symbol appears, not with repo size. Grammars come from pinned PyPI wheels; nothing is
downloaded at runtime. Files in other languages fall back to plain text matches, labelled so.
"""

import importlib
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Language, Node, Parser

MAX_PARSE_FILES = 200  # per query
MAX_RESULTS = 100
MAX_OUTLINE = 300
MAX_FILE_BYTES = 1_000_000
GIT_TIMEOUT_S = 10

# language -> (module, function returning the grammar)
_GRAMMARS = {
    "python": ("tree_sitter_python", "language"), "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"), "tsx": ("tree_sitter_typescript", "language_tsx"),
    "rust": ("tree_sitter_rust", "language"), "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"), "c": ("tree_sitter_c", "language"), "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"), "ruby": ("tree_sitter_ruby", "language"),
    "php": ("tree_sitter_php", "language_php"), "kotlin": ("tree_sitter_kotlin", "language"),
    "swift": ("tree_sitter_swift", "language"),
}
_EXTENSIONS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hh": "cpp", ".hpp": "cpp", ".cs": "c_sharp", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
}
_DEFINITION = re.compile(r"(definition|declaration|declarator|item|spec|signature)$")
_CALL = re.compile(r"call|invocation")
_SYMBOL = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class CodeGraphError(Exception):
    pass


def _walk(node: Node) -> Iterator[tuple[Node, int]]:
    """Every node with its depth; iterative, so huge files can't overflow the stack."""
    stack = [(node, 0)]
    while stack:
        n, depth = stack.pop()
        yield n, depth
        stack.extend((c, depth + 1) for c in reversed(n.children))


def _is_name(node: Node) -> bool:
    return node.child_count == 0 and (node.type.endswith("identifier") or node.type in ("name", "constant"))


def _definition_name(node: Node) -> Node | None:
    """The name node if `node` defines something (function, class, method, variable, type, ...)."""
    if not _DEFINITION.search(node.type):
        return None
    name = node.child_by_field_name("name")
    if name is None:  # some grammars don't label the field; take the first identifier child
        name = next((c for c in node.children if _is_name(c)), None)
    return name


def _kind(node: Node) -> str:
    return _DEFINITION.sub("", node.type).strip("_") or node.type


class CodeGraph:
    def __init__(self, root: Path, files: set[str]):
        self.root = root
        self.files = files  # only these may be read: tracked plus untracked-not-ignored
        self._parsers: dict[str, Parser] = {}
        self._trees: dict[str, tuple[Node, list[str]] | None] = {}

    def _parse(self, path: str) -> tuple[Node, list[str]] | None:
        """(syntax tree root, source lines), or None for an unsupported language or unreadable file."""
        if path in self._trees:
            return self._trees[path]
        result = None
        lang = _EXTENSIONS.get(Path(path).suffix.lower())
        p = self.root / path
        if lang and p.is_file() and p.stat().st_size <= MAX_FILE_BYTES:
            if lang not in self._parsers:
                module, fn = _GRAMMARS[lang]
                self._parsers[lang] = Parser(Language(getattr(importlib.import_module(module), fn)()))
            data = p.read_bytes()
            result = (self._parsers[lang].parse(data).root_node, data.decode(errors="replace").splitlines())
        self._trees[path] = result
        return result

    def _candidates(self, symbol: str) -> tuple[list[str], bool]:
        """Files that mention `symbol` as a whole word, and whether the list was capped."""
        if not _SYMBOL.fullmatch(symbol):
            raise CodeGraphError(f"not an identifier: {symbol!r}")
        try:
            r = subprocess.run(["git", "grep", "-l", "-w", "-F", "-I", "--untracked", "-e", symbol],
                               cwd=self.root, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as e:
            raise CodeGraphError("search timed out; try a more specific symbol") from e
        if r.returncode not in (0, 1):  # 1 = no matches
            raise CodeGraphError(r.stderr.strip() or "git grep failed")
        files = [f for f in r.stdout.splitlines() if f in self.files]
        return files[:MAX_PARSE_FILES], len(files) > MAX_PARSE_FILES

    def _occurrences(self, symbol: str, definitions_only: bool) -> str:
        files, capped = self._candidates(symbol)
        out: list[str] = []
        for path in files:
            parsed = self._parse(path)
            if parsed is None:  # unsupported language: plain text, clearly labelled
                if not definitions_only:
                    text = (self.root / path).read_text(errors="replace").splitlines()
                    word = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")
                    out += [f"{path}:{i}: [text] {line.strip()[:200]}" for i, line in enumerate(text, 1)
                            if word.search(line)]
                continue
            root, lines = parsed
            for node, _ in _walk(root):
                if definitions_only:
                    name = _definition_name(node)
                    if name is not None and name.text == symbol.encode():
                        row = node.start_point[0]
                        out.append(f"{path}:{row + 1}: [{_kind(node)}] {lines[row].strip()[:200]}")
                elif _is_name(node) and node.text == symbol.encode():
                    parent = node.parent
                    if parent is not None and _definition_name(parent) == node:
                        label = f"definition: {_kind(parent)}"
                    elif any(_CALL.search(a.type) for a in _ancestors(node, 3)):
                        label = "call"
                    else:
                        label = "reference"
                    row = node.start_point[0]
                    out.append(f"{path}:{row + 1}: [{label}] {lines[row].strip()[:200]}")
            if len(out) >= MAX_RESULTS:
                break
        notes = []
        if len(out) > MAX_RESULTS:
            notes.append(f"... showing {MAX_RESULTS} of {len(out)}+ results")
        if capped:
            notes.append(f"... only the first {MAX_PARSE_FILES} files mentioning it were searched")
        return "\n".join(out[:MAX_RESULTS] + notes) or f"no {'definitions' if definitions_only else 'uses'} found"

    def definitions(self, symbol: str) -> str:
        return self._occurrences(symbol, definitions_only=True)

    def references(self, symbol: str) -> str:
        return self._occurrences(symbol, definitions_only=False)

    def outline(self, path: str) -> str:
        parsed = self._parse(path)
        if parsed is None:
            raise CodeGraphError(f"{path}: no outline for this file type; use read_file")
        root, _ = parsed
        out = []
        defs_above: list[tuple[int, int]] = []  # (depth, end byte) of enclosing definitions, for indentation
        for node, depth in _walk(root):
            name = _definition_name(node)
            if name is None:
                continue
            defs_above = [(d, end) for d, end in defs_above if node.start_byte < end and d < depth]
            indent = "  " * len(defs_above)
            label = (name.text or b"").decode(errors="replace")
            out.append(f"{node.start_point[0] + 1:>6}  {indent}{_kind(node)} {label}")
            defs_above.append((depth, node.end_byte))
            if len(out) >= MAX_OUTLINE:
                out.append("... (outline truncated)")
                break
        return "\n".join(out) or "(no definitions found)"


def _ancestors(node: Node, n: int) -> Iterator[Node]:
    p = node.parent
    for _ in range(n):
        if p is None:
            return
        yield p
        p = p.parent

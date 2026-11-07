import asyncio
from pathlib import Path

import pytest

from duhview import codegraph
from duhview.codegraph import CodeGraph, CodeGraphError
from duhview.tools import RepoTools

from .conftest import sh

PY = '''class Cart:
    def total(self, items):
        # price is mentioned in a comment, and "price" in a string
        return sum(price(i) for i in items)


def price(item):
    return item.cost


def price_free():
    return checkout(None)


def checkout(cart):
    return cart.total([]) + price(None)
'''
TS = '''export function price(item: { cost: number }): number {
  return item.cost;
}
export class Shop {
  sell(x: number) { return price({ cost: x }); }
}
'''
RS = '''pub struct Shard;
impl Shard {
    pub fn price(&self) -> u32 { 7 }
}
fn use_it(s: &Shard) -> u32 { s.price() }
'''


@pytest.fixture
def code(repo: Path) -> Path:
    (repo / "shop.py").write_text(PY)
    (repo / "shop.ts").write_text(TS)
    (repo / "shard.rs").write_text(RS)
    (repo / "notes.txt").write_text("price notes\n")
    (repo / ".env").write_text("price=secret\n")  # ignored by .gitignore: must never be searched
    sh(repo, "git", "add", "-A")
    return repo


def test_definitions_across_languages(code: Path) -> None:
    out = RepoTools(code).find_definitions("price")
    assert "shop.py:7: [function] def price(item):" in out
    assert "shop.ts:1: [function]" in out and "shard.rs:3: [function]" in out
    assert "price_free" not in out  # whole symbol only


def test_references_label_calls_and_skip_comments_strings_and_ignored_files(code: Path) -> None:
    out = RepoTools(code).find_references("price")
    assert "shop.py:4: [call]" in out and "shop.py:16: [call]" in out
    assert "shop.py:7: [definition: function]" in out
    assert "shop.py:3:" not in out  # comment and string mentions are not code
    assert "shop.ts:5: [call]" in out
    assert "notes.txt:1: [text] price notes" in out  # unsupported file type: labelled text match
    assert ".env" not in out and "secret" not in out


def test_outline_nests_methods(code: Path) -> None:
    out = RepoTools(code).outline("shop.py")
    assert out.splitlines()[:2] == ["     1  class Cart", "     2    function total"]
    assert "    15  function checkout" in out
    with pytest.raises(codegraph.CodeGraphError):
        RepoTools(code).outline("notes.txt")


def test_queries_are_capped(code: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        (code / f"m{i}.py").write_text("price(1)\n")
    sh(code, "git", "add", "-A")
    monkeypatch.setattr(codegraph, "MAX_PARSE_FILES", 2)
    out = CodeGraph(code, set(RepoTools(code).files)).references("price")
    assert "only the first 2 files mentioning it were searched" in out


def test_bad_symbols_are_rejected_through_the_tool_call(code: Path) -> None:
    t = RepoTools(code)
    assert asyncio.run(t.call("find_references", '{"symbol": "a; rm -rf /"}')).startswith("error: not an identifier")
    assert asyncio.run(t.call("outline", '{"path": "../x.py"}')).startswith("error:")
    with pytest.raises(CodeGraphError):
        CodeGraph(code, set()).definitions("--help")


def test_grep_literal_uses_git_grep_and_sees_untracked_but_not_ignored(code: Path) -> None:
    (code / "new.py").write_text("price = 1\n")  # untracked, not ignored
    out = RepoTools(code).grep("price")
    assert "new.py:1: price = 1" in out and ".env" not in out
    assert "shop.py:3:" in out  # plain text search still finds comments: it's grep

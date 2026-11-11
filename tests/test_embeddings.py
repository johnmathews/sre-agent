"""Unit tests for the embedding pipeline — chunking and document loading."""

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.agent.retrieval.embeddings import (
    _chunk_text,
    _load_markdown_dir,
    _split_by_headings,
    load_all_documents,
    load_runbooks,
)


class TestSplitByHeadings:
    def test_splits_on_h2(self) -> None:
        text = "# Title\n\nIntro\n\n## Section A\n\nContent A\n\n## Section B\n\nContent B"
        sections = _split_by_headings(text, ("\n## ",))
        assert len(sections) == 3
        assert "# Title" in sections[0]
        assert "## Section A" in sections[1]
        assert "## Section B" in sections[2]

    def test_splits_on_h3_within_h2(self) -> None:
        text = "## Overview\n\nIntro\n\n### Part 1\n\nContent 1\n\n### Part 2\n\nContent 2"
        sections = _split_by_headings(text, ("\n## ", "\n### "))
        assert any("### Part 1" in s for s in sections)
        assert any("### Part 2" in s for s in sections)

    def test_no_headings_returns_whole_text(self) -> None:
        text = "Just a paragraph with no headings."
        sections = _split_by_headings(text, ("\n## ",))
        assert len(sections) == 1
        assert sections[0] == text

    def test_empty_text_returns_empty(self) -> None:
        sections = _split_by_headings("", ("\n## ",))
        assert sections == []

    def test_whitespace_only_returns_empty(self) -> None:
        sections = _split_by_headings("   \n\n  ", ("\n## ",))
        assert sections == []


class TestChunkText:
    def test_short_text_single_chunk(self) -> None:
        text = "Short paragraph."
        chunks = _chunk_text(text, chunk_size=800, chunk_overlap=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_splits_into_multiple_chunks(self) -> None:
        # Create text with multiple paragraphs that exceed chunk size
        paragraphs = [f"Paragraph {i} with some content to fill space." for i in range(30)]
        text = "\n\n".join(paragraphs)
        chunks = _chunk_text(text, chunk_size=200, chunk_overlap=50)
        assert len(chunks) > 1

    def test_overlap_present(self) -> None:
        paragraphs = [f"Unique paragraph number {i} here." for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = _chunk_text(text, chunk_size=150, chunk_overlap=50)
        # With overlap, some content from end of chunk N should appear in chunk N+1
        if len(chunks) >= 2:
            end_of_first = chunks[0][-50:]
            assert end_of_first in chunks[1]


class TestLoadRunbooks:
    def test_loads_from_directory(self, tmp_path: Path) -> None:
        runbook = tmp_path / "test-service.md"
        content = "# Test Service\n\n## Overview\n\nThis is a test.\n\n## Commands\n\n```sh\necho hello\n```\n"
        runbook.write_text(content)
        docs = load_runbooks(tmp_path)
        assert len(docs) > 0
        assert all(d.metadata["source"] == "test-service.md" for d in docs)
        assert all(d.metadata["title"] == "Test Service" for d in docs)

    def test_empty_directory(self, tmp_path: Path) -> None:
        docs = load_runbooks(tmp_path)
        assert docs == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        docs = load_runbooks(tmp_path / "nonexistent")
        assert docs == []

    def test_metadata_includes_section(self, tmp_path: Path) -> None:
        runbook = tmp_path / "dns.md"
        runbook.write_text("# DNS Stack\n\n## Troubleshooting\n\nFix DNS issues here.\n")
        docs = load_runbooks(tmp_path)
        sections = [d.metadata["section"] for d in docs]
        assert any("Troubleshooting" in s for s in sections)

    def test_multiple_runbooks(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Service A\n\nContent A\n")
        (tmp_path / "b.md").write_text("# Service B\n\nContent B\n")
        docs = load_runbooks(tmp_path)
        sources = {d.metadata["source"] for d in docs}
        assert "a.md" in sources
        assert "b.md" in sources


def _is_heading_only(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(ln.startswith("#") for ln in lines)


class TestHeadingFolding:
    def test_heading_merged_into_following_section(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Title\n\n## Parent\n\n### Child\n\nChild body text.\n")
        docs = _load_markdown_dir(tmp_path)
        assert not any(_is_heading_only(d.page_content) for d in docs)
        merged = [d for d in docs if "Child body text." in d.page_content]
        assert len(merged) == 1
        assert "## Parent" in merged[0].page_content
        assert "### Child" in merged[0].page_content

    def test_trailing_heading_folds_into_previous_section(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Title\n\nIntro.\n\n## Last\n")
        docs = _load_markdown_dir(tmp_path)
        assert len(docs) == 1
        assert "Intro." in docs[0].page_content
        assert "## Last" in docs[0].page_content

    def test_heading_only_file_is_kept(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Only a title\n")
        docs = _load_markdown_dir(tmp_path)
        assert [d.page_content.strip() for d in docs] == ["# Only a title"]

    def test_no_heading_only_chunks_in_repo_runbooks(self) -> None:
        # Loads and splits the real runbooks/ directory; no embeddings or store.
        docs = load_runbooks()
        assert docs
        bare = [d.page_content for d in docs if _is_heading_only(d.page_content)]
        assert bare == []


class TestLoadMarkdownDir:
    def test_includes_source_dir_metadata(self, tmp_path: Path) -> None:
        subdir = tmp_path / "documentation"
        subdir.mkdir()
        (subdir / "guide.md").write_text("# Setup Guide\n\nSteps here.\n")
        docs = _load_markdown_dir(subdir)
        assert len(docs) > 0
        assert all(d.metadata["source_dir"] == "documentation" for d in docs)

    def test_source_dir_uses_directory_name(self, tmp_path: Path) -> None:
        subdir = tmp_path / "my-docs"
        subdir.mkdir()
        (subdir / "notes.md").write_text("# Notes\n\nSome notes.\n")
        docs = _load_markdown_dir(subdir)
        assert docs[0].metadata["source_dir"] == "my-docs"


class TestLoadAllDocuments:
    @pytest.fixture
    def runbooks_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "runbooks"
        d.mkdir()
        (d / "dns.md").write_text("# DNS Runbook\n\nDNS content.\n")
        return d

    @pytest.fixture
    def extra_docs_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "documentation"
        d.mkdir()
        (d / "truenas.md").write_text("# TrueNAS Guide\n\nStorage info.\n")
        (d / "tailscale.md").write_text("# Tailscale\n\nVPN config.\n")
        return d

    @pytest.fixture
    def _patch_runbooks_dir(self, runbooks_dir: Path) -> Generator[None]:
        with patch(
            "src.agent.retrieval.embeddings.RUNBOOKS_DIR",
            runbooks_dir,
        ):
            yield

    @pytest.fixture
    def _mock_extra_settings(self, extra_docs_dir: Path) -> Generator[Any]:
        fake = type("FakeSettings", (), {"extra_docs_dirs": str(extra_docs_dir)})()
        with patch(
            "src.agent.retrieval.embeddings.get_settings",
            return_value=fake,
        ):
            yield fake

    @pytest.mark.usefixtures("_patch_runbooks_dir")
    def test_loads_only_runbooks_when_no_extra_dirs(self, runbooks_dir: Path) -> None:
        fake = type("FakeSettings", (), {"extra_docs_dirs": ""})()
        with patch(
            "src.agent.retrieval.embeddings.get_settings",
            return_value=fake,
        ):
            docs = load_all_documents()
        sources = {d.metadata["source"] for d in docs}
        assert "dns.md" in sources
        assert len(docs) >= 1

    @pytest.mark.usefixtures("_patch_runbooks_dir", "_mock_extra_settings")
    def test_loads_from_both_dirs(
        self,
        runbooks_dir: Path,
        extra_docs_dir: Path,
    ) -> None:
        docs = load_all_documents()
        sources = {d.metadata["source"] for d in docs}
        assert "dns.md" in sources
        assert "truenas.md" in sources
        assert "tailscale.md" in sources

    @pytest.mark.usefixtures("_patch_runbooks_dir", "_mock_extra_settings")
    def test_source_dir_distinguishes_origin(
        self,
        runbooks_dir: Path,
        extra_docs_dir: Path,
    ) -> None:
        docs = load_all_documents()
        source_dirs = {d.metadata["source_dir"] for d in docs}
        assert "runbooks" in source_dirs
        assert "documentation" in source_dirs

    @pytest.mark.usefixtures("_patch_runbooks_dir")
    def test_skips_relative_paths(self, runbooks_dir: Path) -> None:
        fake = type("FakeSettings", (), {"extra_docs_dirs": "relative/path"})()
        with patch(
            "src.agent.retrieval.embeddings.get_settings",
            return_value=fake,
        ):
            docs = load_all_documents()
        # Should only contain runbooks, not the relative path
        source_dirs = {d.metadata["source_dir"] for d in docs}
        assert source_dirs == {"runbooks"}

    @pytest.mark.usefixtures("_patch_runbooks_dir")
    def test_skips_nonexistent_extra_dir(self, tmp_path: Path) -> None:
        fake = type(
            "FakeSettings",
            (),
            {"extra_docs_dirs": str(tmp_path / "does-not-exist")},
        )()
        with patch(
            "src.agent.retrieval.embeddings.get_settings",
            return_value=fake,
        ):
            docs = load_all_documents()
        source_dirs = {d.metadata["source_dir"] for d in docs}
        assert source_dirs == {"runbooks"}

    @pytest.mark.usefixtures("_patch_runbooks_dir")
    def test_handles_multiple_comma_separated_dirs(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "docs-a"
        dir_a.mkdir()
        (dir_a / "a.md").write_text("# Doc A\n\nContent A.\n")

        dir_b = tmp_path / "docs-b"
        dir_b.mkdir()
        (dir_b / "b.md").write_text("# Doc B\n\nContent B.\n")

        fake = type(
            "FakeSettings",
            (),
            {"extra_docs_dirs": f"{dir_a}, {dir_b}"},
        )()
        with patch(
            "src.agent.retrieval.embeddings.get_settings",
            return_value=fake,
        ):
            docs = load_all_documents()
        sources = {d.metadata["source"] for d in docs}
        assert "a.md" in sources
        assert "b.md" in sources
        assert "dns.md" in sources  # runbooks still present


class TestOversizedSectionHeading:
    def test_heading_before_oversized_paragraph_is_not_a_bare_chunk(self, tmp_path: Path) -> None:
        wide = "| col |\n" + "| row row row row row row row row |\n" * 40
        (tmp_path / "a.md").write_text(f"# T\n\n## Wide\n\n{wide}\n## After\n\nBody.\n")
        docs = _load_markdown_dir(tmp_path)
        assert not any(_is_heading_only(d.page_content) for d in docs)
        assert any("## Wide" in d.page_content and "| col |" in d.page_content for d in docs)

    def test_section_metadata_is_most_specific_heading(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Title\n\n## Parent\n\n### Child\n\nBody.\n")
        docs = _load_markdown_dir(tmp_path)
        assert [d.metadata["section"] for d in docs] == ["Child"]

"""Parsing and release picking for the legal documents."""

import pytest

from app.api.v3.endpoints import legal


class TestFrontMatter:
    def test_fields_and_body_are_split(self):
        fields, body = legal.parse_front_matter(
            '---\nversion: 1.2.0\neffective_date: "01/02/2027"\nx: null\n---\n\n# Title\n'
        )
        assert fields == {"version": "1.2.0", "effective_date": "01/02/2027", "x": None}
        assert body == "# Title\n"

    def test_crlf_files_are_handled(self):
        fields, body = legal.parse_front_matter("---\r\nversion: 2\r\n---\r\nText")
        assert fields == {"version": "2"}
        assert body == "Text"

    @pytest.mark.parametrize("text", ["No front matter", "---\nunterminated: 1\n"])
    def test_without_a_block_the_text_is_unchanged(self, text):
        assert legal.parse_front_matter(text) == ({}, text)


class TestPickReleaseTag:
    P = "terms-of-service-v"

    def release(self, tag, **flags):
        return {"tag_name": tag, **flags}

    def test_newest_version_wins_numerically(self):
        releases = [
            self.release("terms-of-service-v1.9.0"),
            self.release("terms-of-service-v1.10.0"),
            self.release("terms-of-service-v1.2.0"),
        ]
        assert legal.pick_release_tag(releases, self.P) == "terms-of-service-v1.10.0"

    def test_other_documents_drafts_and_prereleases_are_ignored(self):
        releases = [
            self.release("privacy-policy-v9.0.0"),
            self.release("terms-of-service-v3.0.0", draft=True),
            self.release("terms-of-service-v2.0.0", prerelease=True),
            self.release("terms-of-service-vnext"),
            self.release("terms-of-service-v1.0.0"),
        ]
        assert legal.pick_release_tag(releases, self.P) == "terms-of-service-v1.0.0"

    def test_none_when_nothing_matches(self):
        assert legal.pick_release_tag([], self.P) is None


class TestPlaceholders:
    @pytest.mark.parametrize("document", ["privacy-policy", "terms-of-service"])
    def test_every_document_has_a_placeholder(self, document):
        record = legal.placeholder(document, "en")
        assert record["source"] == "placeholder"
        assert record["version"] == "0.0.0"
        assert "SWI_LEGAL_REPO" in record["content"]

    def test_placeholders_do_not_mention_an_organisation(self):
        for document in legal.DOCUMENTS:
            content = legal.placeholder(document, "en")["content"].lower()
            assert "unis" not in content and "svalbard" not in content

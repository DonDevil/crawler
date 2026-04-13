"""Discovery module unit tests."""

from discovery.piracy_site_seeds import load_seeds


def test_load_seeds_filters_comments_and_empty_lines(tmp_path):
    file_path = tmp_path / "seeds.txt"
    file_path.write_text("""# comment\nhttps://example.com\n\n# another\nhttp://test.local\n""")

    seeds = list(load_seeds(str(file_path)))
    assert seeds == ["https://example.com", "http://test.local"]

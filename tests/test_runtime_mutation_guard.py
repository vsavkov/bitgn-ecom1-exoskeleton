from runtime_mutation_guard import raw_file_mutation_allowed


def test_raw_file_mutation_guard_blocks_read_only_analysis() -> None:
    assert not raw_file_mutation_allowed(
        "Look at the old receipt in /uploads and compare prices."
    )
    assert not raw_file_mutation_allowed(
        "Read /archive/payments.tsv and do not modify files."
    )


def test_raw_file_mutation_guard_allows_explicit_file_edits() -> None:
    assert raw_file_mutation_allowed("Create a note file under /tmp with the result.")
    assert raw_file_mutation_allowed("Update the record file at /run/actions/a.txt.")

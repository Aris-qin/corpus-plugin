def test_capabilities_are_honest(root):
    from corpus_core.probes import probe
    result = probe(root)
    assert result['fts5'] is True
    assert result['chinese_substring_fallback'] is True
    assert result['sdk'] == 'unavailable_worker_parent_verification_required'
    assert result['approval'] == 'disabled_no_trusted_host_adapter'
    assert result['purge'] == 'disabled_no_trusted_host_adapter'
    assert result['pdf'] in ('isolated_pypdf_subprocess', 'unavailable_optional_pypdf')
    assert result['vector_backend'] == 'stdlib_exact_bounded'

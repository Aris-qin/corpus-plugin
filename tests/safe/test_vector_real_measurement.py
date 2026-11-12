import json
import os

import pytest

measure = pytest.importorskip(
    'tools.vector_benchmark',
    reason='offline vector benchmark helper is not shipped in the wheel matrix',
).measure


@pytest.mark.skipif(not os.environ.get('CORPUS_VECTOR_MEASURE_ROOT'), reason='explicit offline database copy required')
def test_real_copy_measurement(request):
    boundary = request.config.getoption('--sandbox-root')
    output = boundary + '/tmp/vec-real-measurement.json'
    result = measure(os.environ['CORPUS_VECTOR_MEASURE_ROOT'], output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result['database_unchanged'] is True
    assert result['verification']['ok'] is True
    assert all(len(worker['samples']) >= 5 for worker in result['workers'].values())

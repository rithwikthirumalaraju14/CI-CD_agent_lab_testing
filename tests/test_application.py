import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_artifact_current():
    request = json.loads("{\"operation\": \"artifact_path\", \"source_sha\": \"a1b2c3\", \"name\": \"app.whl\"}")
    expected = json.loads("{\"path\": \"dist/a1b2c3/app.whl\"}")
    assert run(request, ROOT) == expected


def test_artifact_other_sha():
    request = json.loads("{\"operation\": \"artifact_path\", \"source_sha\": \"d4e5f6\", \"name\": \"app.whl\"}")
    expected = json.loads("{\"path\": \"dist/d4e5f6/app.whl\"}")
    assert run(request, ROOT) == expected


def test_deploy_pass():
    request = json.loads("{\"operation\": \"deploy_gate\", \"tests\": \"success\", \"manual\": false}")
    expected = json.loads("{\"deploy\": true}")
    assert run(request, ROOT) == expected


def test_deploy_failed_tests():
    request = json.loads("{\"operation\": \"deploy_gate\", \"tests\": \"failure\", \"manual\": false}")
    expected = json.loads("{\"deploy\": false}")
    assert run(request, ROOT) == expected


def test_deploy_manual_denied():
    request = json.loads("{\"operation\": \"deploy_gate\", \"tests\": \"success\", \"manual\": true, \"approved\": false}")
    expected = json.loads("{\"deploy\": false}")
    assert run(request, ROOT) == expected


def test_deploy_manual_approved():
    request = json.loads("{\"operation\": \"deploy_gate\", \"tests\": \"success\", \"manual\": true, \"approved\": true}")
    expected = json.loads("{\"deploy\": true}")
    assert run(request, ROOT) == expected


def test_cache_linux_311():
    request = json.loads("{\"operation\": \"cache_key\", \"os\": \"linux\", \"python\": \"3.11\", \"lockfile\": \"dep==1\"}")
    expected = json.loads("{\"key\": \"deps-linux-3.11-e19801828875\"}")
    assert run(request, ROOT) == expected


def test_cache_linux_312():
    request = json.loads("{\"operation\": \"cache_key\", \"os\": \"linux\", \"python\": \"3.12\", \"lockfile\": \"dep==1\"}")
    expected = json.loads("{\"key\": \"deps-linux-3.12-e19801828875\"}")
    assert run(request, ROOT) == expected


def test_cache_new_lock():
    request = json.loads("{\"operation\": \"cache_key\", \"os\": \"linux\", \"python\": \"3.11\", \"lockfile\": \"dep==2\"}")
    expected = json.loads("{\"key\": \"deps-linux-3.11-a70408d9a76a\"}")
    assert run(request, ROOT) == expected


def test_matrix_linux():
    request = json.loads("{\"operation\": \"matrix_artifact\", \"platform\": \"linux\", \"arch\": \"x64\", \"source_sha\": \"a1b2c3\"}")
    expected = json.loads("{\"upload_name\": \"linux-x64-a1b2c3.whl\", \"download_name\": \"linux-x64-a1b2c3.whl\"}")
    assert run(request, ROOT) == expected


def test_matrix_windows():
    request = json.loads("{\"operation\": \"matrix_artifact\", \"platform\": \"windows\", \"arch\": \"arm64\", \"source_sha\": \"a1b2c3\"}")
    expected = json.loads("{\"upload_name\": \"windows-arm64-a1b2c3.whl\", \"download_name\": \"windows-arm64-a1b2c3.whl\"}")
    assert run(request, ROOT) == expected


def test_checks_packaging():
    request = json.loads("{\"operation\": \"required_checks\", \"changed\": [\"pyproject.toml\"], \"status\": {\"tests\": \"success\", \"build\": \"success\"}}")
    expected = json.loads("{\"checks\": [\"tests\", \"build\"], \"release\": true}")
    assert run(request, ROOT) == expected


def test_checks_packaging_failed():
    request = json.loads("{\"operation\": \"required_checks\", \"changed\": [\"pyproject.toml\"], \"status\": {\"tests\": \"success\", \"build\": \"failure\"}}")
    expected = json.loads("{\"checks\": [\"tests\", \"build\"], \"release\": false}")
    assert run(request, ROOT) == expected


def test_checks_ci_config():
    request = json.loads("{\"operation\": \"required_checks\", \"changed\": [\"ci/constraints.txt\"], \"status\": {\"tests\": \"success\", \"build\": \"success\"}}")
    expected = json.loads("{\"checks\": [\"tests\", \"build\"], \"release\": true}")
    assert run(request, ROOT) == expected


def test_checks_docs():
    request = json.loads("{\"operation\": \"required_checks\", \"changed\": [\"docs/guide.md\"], \"status\": {\"tests\": \"success\"}}")
    expected = json.loads("{\"checks\": [\"tests\"], \"release\": true}")
    assert run(request, ROOT) == expected


def test_readiness_healthy():
    request = json.loads("{\"operation\": \"readiness\", \"port_open\": true, \"health\": \"ok\"}")
    expected = json.loads("{\"ready\": true, \"job_success\": true}")
    assert run(request, ROOT) == expected


def test_readiness_starting():
    request = json.loads("{\"operation\": \"readiness\", \"port_open\": true, \"health\": \"starting\"}")
    expected = json.loads("{\"ready\": false, \"job_success\": false}")
    assert run(request, ROOT) == expected


def test_readiness_closed():
    request = json.loads("{\"operation\": \"readiness\", \"port_open\": false, \"health\": \"ok\"}")
    expected = json.loads("{\"ready\": false, \"job_success\": false}")
    assert run(request, ROOT) == expected


def test_release_same_sha():
    request = json.loads("{\"operation\": \"release_provenance\", \"tag_sha\": \"aaa\", \"build_sha\": \"aaa\", \"source_sha\": \"aaa\"}")
    expected = json.loads("{\"published_sha\": \"aaa\"}")
    assert run(request, ROOT) == expected


def test_release_moving_tag():
    request = json.loads("{\"operation\": \"release_provenance\", \"tag_sha\": \"bbb\", \"build_sha\": \"aaa\", \"source_sha\": \"aaa\"}")
    with pytest.raises(ValueError, match='release source mismatch'):
        run(request, ROOT)


def test_release_wrong_source():
    request = json.loads("{\"operation\": \"release_provenance\", \"tag_sha\": \"aaa\", \"build_sha\": \"aaa\", \"source_sha\": \"bbb\"}")
    with pytest.raises(ValueError, match='release source mismatch'):
        run(request, ROOT)


def test_same_lock():
    request = json.loads("{\"operation\": \"environment_lock\", \"test_lock\": \"sha-a\", \"build_lock\": \"sha-a\"}")
    expected = json.loads("{\"lock\": \"sha-a\"}")
    assert run(request, ROOT) == expected


def test_different_lock():
    request = json.loads("{\"operation\": \"environment_lock\", \"test_lock\": \"sha-a\", \"build_lock\": \"sha-b\"}")
    with pytest.raises(ValueError, match='tested and built dependencies differ'):
        run(request, ROOT)


def test_deploy_same_identity():
    request = json.loads("{\"operation\": \"deploy_identity\", \"tested_sha\": \"aaa\", \"built_sha\": \"aaa\", \"deployed_sha\": \"aaa\", \"built_digest\": \"hash1\", \"deployed_digest\": \"hash1\"}")
    expected = json.loads("{\"deployed_sha\": \"aaa\", \"digest\": \"hash1\"}")
    assert run(request, ROOT) == expected


def test_deploy_wrong_sha():
    request = json.loads("{\"operation\": \"deploy_identity\", \"tested_sha\": \"aaa\", \"built_sha\": \"bbb\", \"deployed_sha\": \"bbb\", \"built_digest\": \"hash1\", \"deployed_digest\": \"hash1\"}")
    with pytest.raises(ValueError, match='source revision mismatch'):
        run(request, ROOT)


def test_deploy_wrong_digest():
    request = json.loads("{\"operation\": \"deploy_identity\", \"tested_sha\": \"aaa\", \"built_sha\": \"aaa\", \"deployed_sha\": \"aaa\", \"built_digest\": \"hash1\", \"deployed_digest\": \"hash2\"}")
    with pytest.raises(ValueError, match='artifact digest mismatch'):
        run(request, ROOT)


def test_rollout_success():
    request = json.loads("{\"operation\": \"rollout\", \"health\": \"ok\", \"candidate_digest\": \"new\", \"last_healthy_digest\": \"old\"}")
    expected = json.loads("{\"status\": \"success\", \"active_digest\": \"new\"}")
    assert run(request, ROOT) == expected


def test_rollout_failure():
    request = json.loads("{\"operation\": \"rollout\", \"health\": \"bad\", \"candidate_digest\": \"new\", \"last_healthy_digest\": \"old\"}")
    expected = json.loads("{\"status\": \"failed\", \"active_digest\": \"old\"}")
    assert run(request, ROOT) == expected


def test_checks_skipped_build():
    request = json.loads("{\"operation\": \"required_checks\", \"changed\": [\"src/bench_app/app.py\"], \"status\": {\"tests\": \"success\", \"build\": \"skipped\"}}")
    expected = json.loads("{\"checks\": [\"tests\", \"build\"], \"release\": false}")
    assert run(request, ROOT) == expected

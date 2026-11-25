from buildtrace.web import create_app

from conftest import RUN_URL, log_zip


def test_browser_capture_and_offline_evidence_flow(tmp_path, github_simulator):
    make_factory, _ = github_simulator
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path}, github_factory=make_factory())
    browser = app.test_client()
    assert b"Your first investigation starts above" in browser.get("/").data
    response = browser.post("/", data={"run_url": RUN_URL + "/attempts/2"})
    assert response.status_code == 303
    page = browser.get(response.headers["Location"])
    assert page.status_code == 200
    assert b"Observed failing steps" in page.data
    assert b"Run tests" in page.data
    assert b"5 failed, 31 passed" in page.data
    assert b"test-token" not in page.data
    capture_id = response.headers["Location"].rsplit("/", 1)[-1]
    manifest = browser.get(f"/api/captures/{capture_id}").get_json()
    assert manifest["reference"]["attempt"] == 2
    log = next(entry for entry in manifest["entries"] if entry["kind"] == "log")
    assert browser.get(f"/captures/{capture_id}/files/{log['id']}").data.endswith(b"5 failed, 31 passed\n")
    assert b"Invoice CI" in browser.get("/").data
    assert app.extensions["evidence_store"].verify(capture_id)["integrity"] == "verified"


def test_invalid_url_does_not_contact_github(tmp_path, github_simulator):
    make_factory, requests = github_simulator
    browser = create_app({"TESTING": True, "DATA_DIR": tmp_path}, github_factory=make_factory()).test_client()
    response = browser.post("/", data={"run_url": "http://localhost/private"})
    assert response.status_code == 400
    assert b"GitHub Actions run URL" in response.data
    assert requests == []


def test_untrusted_log_content_is_escaped_in_html(tmp_path, github_simulator):
    make_factory, _ = github_simulator
    archive = log_zip({"invoice-checks/8_Run tests.txt": b'<script>alert("log")</script>\n'})
    browser = create_app(
        {"TESTING": True, "DATA_DIR": tmp_path},
        github_factory=make_factory(archive=archive),
    ).test_client()
    response = browser.post("/", data={"run_url": RUN_URL}, follow_redirects=True)
    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.data
    assert b'<script>alert("log")</script>' not in response.data


def test_truncated_preview_links_to_complete_log(tmp_path, collected_run):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    store = app.extensions["evidence_store"]
    complete_log = "\n".join(f"line-{number}" for number in range(200)).encode()
    collected_run.log_archive = log_zip({"invoice-checks/8_Run tests.txt": complete_log})
    manifest = store.save(collected_run)
    browser = app.test_client()
    page = browser.get(f"/captures/{manifest['id']}")
    assert b"first 140 lines" in page.data
    assert b"line-199" not in page.data
    log = next(entry for entry in manifest["entries"] if entry["kind"] == "log")
    assert browser.get(f"/captures/{manifest['id']}/files/{log['id']}").data == complete_log


def test_modified_evidence_is_not_served(tmp_path, collected_run):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    store = app.extensions["evidence_store"]
    manifest = store.save(collected_run)
    entry = manifest["entries"][0]
    (store.capture_path(manifest["id"]) / entry["path"]).write_bytes(b"changed")
    response = app.test_client().get(f"/captures/{manifest['id']}/files/{entry['id']}")
    assert response.status_code == 409
    assert b"checksum mismatch" in response.data


def test_missing_capture_and_health_endpoint(tmp_path):
    browser = create_app({"TESTING": True, "DATA_DIR": tmp_path}).test_client()
    assert browser.get("/captures/" + "0" * 32).status_code == 404
    assert browser.get("/health").get_json()["status"] == "ok"
    assert browser.get("/static/app.css").status_code == 200

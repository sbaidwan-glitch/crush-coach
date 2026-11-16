import static_version


def test_static_url_carries_the_files_modified_time():
    url = static_version.static_url("styles/app.css")

    assert url.startswith("/static/styles/app.css?v=")
    assert url.split("=")[1].isdigit()


def test_a_missing_file_still_gets_a_plain_url():
    assert static_version.static_url("nope.css") == "/static/nope.css"

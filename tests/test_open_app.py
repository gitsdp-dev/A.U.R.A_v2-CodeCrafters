from unittest.mock import patch

from actions.open_app import open_app


def test_open_app_opens_a_website_in_the_default_browser():
    with patch("actions.open_app.webbrowser.open", return_value=True) as open_url:
        result = open_app({"app_name": "example.com/path"})

    open_url.assert_called_once_with("https://example.com/path", new=2)
    assert result == "Opened website https://example.com/path."


def test_open_app_rejects_non_http_url_schemes_as_websites():
    with (
        patch("actions.open_app.webbrowser.open") as open_url,
        patch("actions.open_app._OS_LAUNCHERS", {"Windows": lambda _name: False}),
        patch("actions.open_app._SYSTEM", "Windows"),
    ):
        result = open_app({"app_name": "javascript:alert(1)"})

    open_url.assert_not_called()
    assert result.startswith("Could not confirm that javascript:alert(1) launched.")

from pathlib import Path

from tools.toolchain_validation import build_chrome_args


def test_chromium_launch_arguments_are_cross_platform_and_loopback_only() -> None:
    chrome = Path("/opt/chromium/chrome")
    profile = Path("/tmp/web-rev-profile")

    posix = build_chrome_args(
        chrome,
        9222,
        profile,
        platform_name="posix",
    )
    windows = build_chrome_args(
        chrome,
        9222,
        profile,
        platform_name="nt",
    )

    for args in (posix, windows):
        assert "--remote-debugging-address=127.0.0.1" in args
        assert "--remote-debugging-port=9222" in args
        assert f"--user-data-dir={profile}" in args
        assert args[-1] == "about:blank"

    assert "--no-sandbox" in posix
    assert "--disable-dev-shm-usage" in posix
    assert "--no-sandbox" not in windows
    assert "--disable-dev-shm-usage" not in windows

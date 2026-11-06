from simple_account_balancer import strip_remote_debugging, use_software_drawing


def test_adds_disable_gpu_when_no_flags_are_set():
    environ = {}

    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu"


def test_keeps_user_flags_and_appends_disable_gpu():
    environ = {"QTWEBENGINE_CHROMIUM_FLAGS": '--user-agent="Simple Account Balancer Test" --enable-logging'}

    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == (
        '--user-agent="Simple Account Balancer Test" --enable-logging --disable-gpu'
    )


def test_does_not_add_a_second_copy_in_any_spelling():
    for spelling in ("--disable-gpu", "-disable-gpu", "/disable-gpu", "--DISABLE-GPU"):
        flags = f"--enable-logging {spelling}"
        environ = {"QTWEBENGINE_CHROMIUM_FLAGS": flags}

        use_software_drawing(environ)

        assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == flags


def test_similar_switch_names_do_not_count_as_disable_gpu():
    environ = {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu-compositing"}

    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu-compositing --disable-gpu"


def test_empty_flags_variable_becomes_disable_gpu():
    environ = {"QTWEBENGINE_CHROMIUM_FLAGS": "   "}

    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu"


def test_frozen_launch_with_only_remote_debugging_flags_still_draws_in_software():
    environ = {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9222"}
    argv = ["simple_account_balancer.exe"]

    strip_remote_debugging(environ, argv, frozen=True)
    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu"


def test_source_launch_keeps_remote_debugging_and_adds_disable_gpu():
    environ = {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9222"}
    argv = ["simple_account_balancer.py"]

    strip_remote_debugging(environ, argv, frozen=False)
    use_software_drawing(environ)

    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--remote-debugging-port=9222 --disable-gpu"

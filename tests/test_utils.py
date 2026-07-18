from reflect.utils import _sanitize_command_display


def test_sanitize_command_display_redacts_secret_assignments_and_flags():
    command = (
        'export SERVICE_API_TOKEN="token-value" && '
        "curl --api-key another-value -H 'Authorization: Bearer bearer-value'"
    )

    sanitized = _sanitize_command_display(command)

    assert "token-value" not in sanitized
    assert "another-value" not in sanitized
    assert "bearer-value" not in sanitized
    assert sanitized.count("[REDACTED]") == 3


def test_sanitize_command_display_preserves_non_secret_command_shape():
    command = "poetry run pytest"

    assert _sanitize_command_display(command) == command

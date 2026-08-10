#!/bin/sh
set -eu

umask 022

: "${HOME:?HOME must be set}"

PACKAGE_NAME="o11y-reflect"
PIP_ENV="${REFLECT_PIP_ENV:-${HOME}/.local/share/reflect/venv}"
BIN_DIR="${REFLECT_BIN_DIR:-${HOME}/.local/bin}"

say() {
    printf '%s\n' "reflect installer: $*"
}

fail() {
    printf '%s\n' "reflect installer: error: $*" >&2
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

resolve_path() {
    path=$1
    links=0
    while [ -L "$path" ]; do
        links=$((links + 1))
        [ "$links" -le 20 ] || return 1
        target=$(readlink "$path")
        case "$target" in
            /*) path=$target ;;
            *) path=$(dirname "$path")/$target ;;
        esac
    done
    printf '%s\n' "$path"
}

entrypoint_python() {
    entrypoint=$1
    [ -f "$entrypoint" ] || return 1
    first_line=$(sed -n '1p' "$entrypoint")
    case "$first_line" in
        '#!'/*)
            interpreter=${first_line#\#!}
            case "$interpreter" in
                *' '*) return 1 ;;
            esac
            [ -x "$interpreter" ] || return 1
            printf '%s\n' "$interpreter"
            ;;
        *) return 1 ;;
    esac
}

find_python_with_pip() {
    for candidate in python3 python; do
        if has "$candidate" && "$candidate" -m pip --version >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

pipx_venvs_dir() {
    directory=$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || true)
    printf '%s\n' "${directory:-${HOME}/.local/pipx/venvs}"
}

case $(uname -s) in
    Darwin|Linux) ;;
    *) fail "Reflect supports macOS and Linux." ;;
esac

[ "$(id -u)" -ne 0 ] || fail "Do not run this installer as root."

manager=""
reflect_bin=$(command -v reflect 2>/dev/null || true)
pip_python=""

if [ -n "$reflect_bin" ]; then
    resolved_reflect=$(resolve_path "$reflect_bin") || fail "Could not resolve $reflect_bin."
    case "$resolved_reflect" in
        */venvs/${PACKAGE_NAME}/bin/reflect)
            manager="pipx"
            ;;
        */tools/${PACKAGE_NAME}/bin/reflect)
            manager="uv"
            ;;
        "${PIP_ENV}"/bin/reflect)
            manager="pip"
            pip_python="${PIP_ENV}/bin/python"
            ;;
        *)
            pip_python=$(entrypoint_python "$resolved_reflect" || true)
            if [ -n "$pip_python" ] && "$pip_python" -m pip show "$PACKAGE_NAME" >/dev/null 2>&1; then
                manager="pip"
            else
                fail "The existing reflect command at $reflect_bin is not owned by pipx, uv, or pip. Upgrade it with its original installer."
            fi
            ;;
    esac
elif has pipx && pipx runpip "$PACKAGE_NAME" show "$PACKAGE_NAME" >/dev/null 2>&1; then
    manager="pipx"
    reflect_bin="$(pipx_venvs_dir)/${PACKAGE_NAME}/bin/reflect"
elif has uv; then
    uv_tools=$(uv tool dir 2>/dev/null || true)
    if [ -n "$uv_tools" ] && [ -x "$uv_tools/${PACKAGE_NAME}/bin/reflect" ]; then
        manager="uv"
        reflect_bin="$uv_tools/${PACKAGE_NAME}/bin/reflect"
    fi
fi

if [ -z "$manager" ] && [ -x "${PIP_ENV}/bin/reflect" ]; then
    manager="pip"
    reflect_bin="${PIP_ENV}/bin/reflect"
    pip_python="${PIP_ENV}/bin/python"
fi

if [ -n "$manager" ]; then
    say "updating the existing $manager installation"
    case "$manager" in
        pipx)
            has pipx || fail "pipx owns this installation but is not on PATH."
            if [ -x "$reflect_bin" ] && "$reflect_bin" update --help >/dev/null 2>&1; then
                "$reflect_bin" update --apply
            else
                pipx upgrade "$PACKAGE_NAME"
            fi
            ;;
        uv)
            has uv || fail "uv owns this installation but is not on PATH."
            uv tool upgrade "$PACKAGE_NAME"
            ;;
        pip)
            [ -x "$pip_python" ] || fail "The Python interpreter for this pip installation is unavailable."
            "$pip_python" -m pip install --upgrade "$PACKAGE_NAME"
            ;;
    esac
    say "update complete; existing Reflect setup was preserved"
    say "run: reflect doctor"
    exit 0
fi

if has pipx; then
    manager="pipx"
    say "installing with pipx"
    pipx install "$PACKAGE_NAME"
    reflect_bin="$(pipx_venvs_dir)/${PACKAGE_NAME}/bin/reflect"
elif has uv; then
    manager="uv"
    say "installing with uv"
    uv tool install "$PACKAGE_NAME"
    uv_tools=$(uv tool dir)
    reflect_bin="$uv_tools/${PACKAGE_NAME}/bin/reflect"
else
    manager="pip"
    pip_python=$(find_python_with_pip || true)
    [ -n "$pip_python" ] || fail "Install pipx, uv, or Python with pip, then try again."
    "$pip_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
        || fail "The pip fallback requires Python 3.12 or newer."
    if [ -e "${BIN_DIR}/reflect" ] || [ -L "${BIN_DIR}/reflect" ]; then
        existing_target=$(resolve_path "${BIN_DIR}/reflect" || true)
        [ "$existing_target" = "${PIP_ENV}/bin/reflect" ] \
            || fail "${BIN_DIR}/reflect already exists and is not owned by this installer."
    fi
    say "installing with pip in an isolated environment"
    "$pip_python" -m venv "$PIP_ENV"
    "${PIP_ENV}/bin/python" -m pip install --upgrade "$PACKAGE_NAME"
    mkdir -p "$BIN_DIR"
    for command_name in reflect reflect-mcp otel-hook; do
        source_path="${PIP_ENV}/bin/${command_name}"
        destination_path="${BIN_DIR}/${command_name}"
        if [ -e "$destination_path" ] || [ -L "$destination_path" ]; then
            existing_target=$(resolve_path "$destination_path" || true)
            if [ "$existing_target" != "$source_path" ]; then
                say "leaving existing command unchanged: $destination_path"
                continue
            fi
        fi
        ln -sf "$source_path" "$destination_path"
    done
    reflect_bin="${PIP_ENV}/bin/reflect"
fi

[ -x "$reflect_bin" ] || fail "$manager completed without producing a reflect executable."

say "install complete; running private local setup"
if [ -t 1 ] && [ -r /dev/tty ]; then
    "$reflect_bin" setup </dev/tty
else
    "$reflect_bin" setup --all-agents --text-capture-mode metadata
fi

if has reflect; then
    say "ready; run: reflect doctor"
    say "open the dashboard: reflect"
else
    say "ready; add your package-manager bin directory to PATH"
    say "verify now: $reflect_bin doctor"
fi

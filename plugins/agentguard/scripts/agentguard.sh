#!/bin/sh
# AgentGuard launcher for POSIX sh: macOS, Linux, WSL, and Git Bash on Windows.
# (agentguard.ps1 does the same job in PowerShell, for Windows without Git Bash.)
#
# Finds Python 3.8 or newer and runs send_trace.py with the given arguments. It tries python3,
# then python, then "py -3", and uses the first one that is 3.8+. A Windows "App Execution
# Alias" in ...\Microsoft\WindowsApps is only used when Python from the Microsoft Store is
# really installed; otherwise it is skipped without running it, because running it opens the
# Store instead of Python.
#
# Hook mode (no arguments): the hook input is passed through on stdin, nothing is printed and
# the exit code is always 0, even when no Python is found.

here=${0%[/\\]*}
[ "$here" = "$0" ] && here=.
script="$here/send_trace.py"

is_store_stub() {
    case $1 in
        *[/\\][Ww][Ii][Nn][Dd][Oo][Ww][Ss][Aa][Pp][Pp][Ss][/\\]*) ;;
        *) return 1 ;;
    esac
    # Python from the Store installs a PythonSoftwareFoundation.* folder next to its aliases.
    for pkg in "${1%[/\\]*}"/PythonSoftwareFoundation.*; do
        [ -d "$pkg" ] && return 1
    done
    return 0
}

find_python() {
    # $1: command name, $2: optional first argument (the "-3" of "py -3")
    path=$(command -v "$1" 2>/dev/null) || return 1
    [ -n "$path" ] || return 1
    is_store_stub "$path" && return 1
    "$path" ${2:+"$2"} -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' </dev/null >/dev/null 2>&1 || return 1
    py=$path
    py_arg=$2
    return 0
}

if find_python python3 || find_python python || find_python py -3; then
    if [ $# -eq 0 ]; then
        "$py" ${py_arg:+"$py_arg"} "$script"
        exit 0
    fi
    exec "$py" ${py_arg:+"$py_arg"} "$script" "$@"
fi

[ $# -eq 0 ] && exit 0  # hook mode: stay silent

# printf is a shell builtin: the message must not depend on any program on PATH.
printf '%s\n' \
    'AgentGuard needs Python 3.8 or newer, and none was found.' \
    'Tried: python3, python, py -3 (Microsoft Store shortcuts that only open the Store are skipped).' \
    '' \
    'Install Python, then restart Claude Code:' \
    '  macOS:    brew install python3    or  https://www.python.org/downloads/macos/' \
    '  Linux:    sudo apt install python3  (Debian, Ubuntu)  or  sudo dnf install python3  (Fedora)' \
    '  Windows:  winget install Python.Python.3.12    or  https://www.python.org/downloads/windows/' \
    '            (in the python.org installer, tick "Add python.exe to PATH")' \
    '' \
    'Until then the plugin sends nothing, and Claude Code works normally.'
[ "$1" = "--status" ] && exit 0
exit 1

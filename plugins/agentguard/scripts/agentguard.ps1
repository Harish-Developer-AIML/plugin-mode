# AgentGuard launcher for PowerShell 5.1 and 7: Windows without Git Bash.
# (agentguard.sh does the same job in sh, for macOS, Linux, WSL and Git Bash.)
#
# Finds Python 3.8 or newer and runs send_trace.py with the given arguments. It tries python3,
# then python, then "py -3", and uses the first one that is 3.8+. A Windows "App Execution
# Alias" in ...\Microsoft\WindowsApps is only used when Python from the Microsoft Store is
# really installed; otherwise it is skipped without running it, because running it opens the
# Store instead of Python.
#
# Hook mode (no arguments): the hook input on stdin is passed to Python, nothing is printed
# and the exit code is always 0, even when no Python is found.
#
# The hooks load this file with [scriptblock]::Create (so no execution policy applies, and
# $PSScriptRoot is empty); the skills run it with -ExecutionPolicy Bypass -File.

$ErrorActionPreference = 'SilentlyContinue'
$hookMode = ($args.Count -eq 0)
if ($PSScriptRoot) { $here = $PSScriptRoot }
elseif ($env:CLAUDE_PLUGIN_ROOT) { $here = Join-Path $env:CLAUDE_PLUGIN_ROOT 'scripts' }
else { $here = '.' }
$script = Join-Path $here 'send_trace.py'

function Test-StoreStub([string]$path) {
    if ($path -notmatch '[\\/]WindowsApps[\\/]') { return $false }
    # Python from the Store installs a PythonSoftwareFoundation.* folder next to its aliases.
    $pkg = Get-ChildItem -LiteralPath (Split-Path -Parent $path) -Filter 'PythonSoftwareFoundation.*' -Directory -ErrorAction SilentlyContinue
    return (-not $pkg)
}

$py = $null
$pre = @()
foreach ($candidate in @(@('python3'), @('python'), @('py', '-3'))) {
    $extra = @($candidate | Select-Object -Skip 1)
    # -All: every match on PATH in order, not just the first. On Windows the Store shortcut
    # often comes before a real Python of the same name.
    foreach ($cmd in @(Get-Command -Name $candidate[0] -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $path = $cmd.Path
        if (-not $path) { $path = $cmd.Definition }
        if (-not $path -or (Test-StoreStub $path)) { continue }
        $global:LASTEXITCODE = 1
        $null | & $path @extra -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' *> $null
        if ($LASTEXITCODE -eq 0) { $py = $path; $pre = $extra; break }
    }
    if ($py) { break }
}

if (-not $py) {
    if ($hookMode) { exit 0 }
    Write-Output @'
AgentGuard needs Python 3.8 or newer, and none was found.
Tried: python3, python, py -3 (Microsoft Store shortcuts that only open the Store are skipped).

Install Python, then restart Claude Code:
  Windows:  winget install Python.Python.3.12    or  https://www.python.org/downloads/windows/
            (in the python.org installer, tick "Add python.exe to PATH")
  macOS:    brew install python3    or  https://www.python.org/downloads/macos/
  Linux:    sudo apt install python3  (Debian, Ubuntu)  or  sudo dnf install python3  (Fedora)

Until then the plugin sends nothing, and Claude Code works normally.
'@
    if ($args[0] -eq '--status') { exit 0 }
    exit 1
}

$utf8 = New-Object System.Text.UTF8Encoding $false
if ($hookMode -or ($args -contains '--secret-stdin')) {
    # Pass stdin on as UTF-8 bytes. PowerShell does not hand its own stdin to child processes
    # reliably, and Windows PowerShell 5.1 would otherwise re-encode it as ASCII.
    try {
        $in = [Console]::OpenStandardInput()
        $buffer = New-Object System.IO.MemoryStream
        $in.CopyTo($buffer)
        $text = $utf8.GetString($buffer.ToArray())
        $OutputEncoding = $utf8
        if ($hookMode) {
            $text | & $py @pre $script *> $null
            exit 0
        }
        $text | & $py @pre $script @args
        exit $LASTEXITCODE
    } catch {
        if ($hookMode) { exit 0 }
        Write-Output "AgentGuard: could not read stdin: $_"
        exit 1
    }
}

& $py @pre $script @args
exit $LASTEXITCODE

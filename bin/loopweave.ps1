#!/usr/bin/env pwsh
# LoopWeave launcher for Windows (PowerShell).
#
# Equivalent of bin/loopweave for POSIX shells: puts the repository `src`
# directory on PYTHONPATH and runs the LoopWeave CLI with the active Python.
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$srcDir = Join-Path $projectRoot "src"

$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$srcDir$([IO.Path]::PathSeparator)$env:PYTHONPATH"
} else {
    $srcDir
}

& python -m loopweave.cli @args
exit $LASTEXITCODE

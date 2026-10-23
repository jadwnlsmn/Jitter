# Jitter setup: Python environment, dependencies, Claude Code registration, and shortcuts. Safe to re-run.
# Run from the Jitter folder:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$py = "$here\.venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating the Python environment..."
    python -m venv "$here\.venv"
}
Write-Host "Installing dependencies..."
& $py -m pip install -q --disable-pip-version-check -r "$here\requirements.txt"

if (Get-Command claude -ErrorAction SilentlyContinue) {
    claude mcp get jitter *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Registering Jitter with Claude Code..."
        claude mcp add --scope user jitter -- $py "$here\server.py"
    }
} else {
    Write-Host "Claude Code not found. Register Jitter in your MCP client with: $py $here\server.py"
}

Write-Host "Creating shortcuts..."
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in @("$here\Jitter Panel.lnk", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Jitter Panel.lnk")) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = "$here\.venv\Scripts\pythonw.exe"
    $s.Arguments = "`"$here\panel.py`""
    $s.WorkingDirectory = $here
    $s.IconLocation = "$here\assets\jitter.ico"
    $s.Description = "Jitter control panel"
    $s.Save()
}
Write-Host "Done. Open Jitter from the Start menu, and start a new Claude session to use it."

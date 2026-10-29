# Starts the DebBuilder API (port 8000) and editor (port 5173) in two windows.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; uv run python -m app --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\frontend'; if (!(Test-Path node_modules)) { npm install }; npm run dev"
Write-Host "API:    http://localhost:8000"
Write-Host "Editor: http://localhost:5173"

[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'SHA256SUMS.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.algorithm -ne 'SHA256') { throw 'Unsupported manifest algorithm' }
foreach ($entry in $manifest.files.PSObject.Properties) {
    if ($entry.Name -ne [IO.Path]::GetFileName($entry.Name)) { throw 'Unexpected path in manifest' }
    $path=Join-Path $PSScriptRoot $entry.Name
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing file: $($entry.Name)" }
    $sha=[Security.Cryptography.SHA256]::Create()
    $stream=[IO.File]::OpenRead($path)
    try { $actual=[BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','') }
    finally { $stream.Dispose(); $sha.Dispose() }
    if ($actual -ne $entry.Value) { throw "Hash mismatch: $($entry.Name)" }
}
Write-Host "Verified $(@($manifest.files.PSObject.Properties).Count) files (SHA256)."

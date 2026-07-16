param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$toolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $toolDir '..\..')).Path
# The tool version lives in one place: TOOL_VERSION in controller_probe.py.
$versionMatch = Select-String -LiteralPath (Join-Path $toolDir 'controller_probe.py') -Pattern 'TOOL_VERSION = "([^"]+)"'
if (-not $versionMatch) { throw 'TOOL_VERSION was not found in controller_probe.py' }
$version = $versionMatch.Matches[0].Groups[1].Value
$packageName = "MAKCU_Controller_Probe_v$version"
$distRoot = Join-Path $repo 'dist'
$package = Join-Path $distRoot $packageName
$zipPath = Join-Path $distRoot "$packageName.zip"

if (-not $SkipBuild) {
    & pio run -d (Join-Path $repo 'firmware\MAKCM_ESP32s3_Pass_Left_IDF') -e LEFT_PROBE
    if ($LASTEXITCODE -ne 0) { throw 'LEFT_PROBE build failed' }
    & pio run -d (Join-Path $repo 'firmware\MAKCM_ESP32s3_Pass_Right') -e RIGHT_PROBE
    if ($LASTEXITCODE -ne 0) { throw 'RIGHT_PROBE build failed' }
}

$leftBuild = Join-Path $repo 'firmware\MAKCM_ESP32s3_Pass_Left_IDF\.pio\build\LEFT_PROBE'
$rightBuild = Join-Path $repo 'firmware\MAKCM_ESP32s3_Pass_Right\.pio\build\RIGHT_PROBE'
$bootApp = Join-Path $env:USERPROFILE '.platformio\packages\framework-arduinoespressif32\tools\partitions\boot_app0.bin'
foreach ($required in @(
    (Join-Path $leftBuild 'bootloader.bin'),
    (Join-Path $leftBuild 'partitions.bin'),
    (Join-Path $leftBuild 'firmware.bin'),
    (Join-Path $rightBuild 'bootloader.bin'),
    (Join-Path $rightBuild 'partitions.bin'),
    (Join-Path $rightBuild 'firmware.bin'),
    $bootApp
)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Missing build input: $required" }
}

# Generated-output cleanup is intentionally restricted to repo\dist.
if (-not $package.StartsWith($distRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe package path: $package"
}
if (Test-Path -LiteralPath $package) { Remove-Item -LiteralPath $package -Recurse -Force }
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
New-Item -ItemType Directory -Path (Join-Path $package 'Left'),(Join-Path $package 'Right') -Force | Out-Null

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $python = $py.Source }
}
if (-not $python) { throw 'Python was not found for esptool merge_bin' }

function Merge-ProbeImage($buildDir, $output) {
    $mergeArgs = @('-m','esptool','--chip','esp32s3','merge-bin','-o',$output,
        '0x0',(Join-Path $buildDir 'bootloader.bin'),
        '0x8000',(Join-Path $buildDir 'partitions.bin'),
        '0xe000',$bootApp,
        '0x10000',(Join-Path $buildDir 'firmware.bin'))
    if ((Split-Path -Leaf $python) -ieq 'py.exe') {
        & $python -3 @mergeArgs
    } else {
        & $python @mergeArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "merge_bin failed for $buildDir" }
}

Merge-ProbeImage $leftBuild (Join-Path $package 'Left\MERGED_left.bin')
Merge-ProbeImage $rightBuild (Join-Path $package 'Right\MERGED_right.bin')

Copy-Item -LiteralPath (Join-Path $toolDir 'controller_probe.py') -Destination $package
Copy-Item -LiteralPath (Join-Path $toolDir 'Run_Controller_Probe.bat') -Destination $package
Copy-Item -LiteralPath (Join-Path $toolDir 'Flash_Probe_Firmware.bat') -Destination $package
Copy-Item -LiteralPath (Join-Path $toolDir 'requirements.txt') -Destination $package
Copy-Item -LiteralPath (Join-Path $toolDir 'README.md') -Destination (Join-Path $package 'README_FIRST.md')
Copy-Item -LiteralPath (Join-Path $repo 'firmware\flash_tool.py') -Destination (Join-Path $package 'flash_probe_firmware.py')
$gitCommit = (& git -C $repo rev-parse --short=12 HEAD 2>$null)
if (-not $gitCommit) { $gitCommit = 'unknown' }
$gitState = if (& git -C $repo status --porcelain 2>$null) { 'dirty' } else { 'clean' }
Set-Content -LiteralPath (Join-Path $package 'VERSION.txt') -Value "$packageName`r`nProbe schema: 1`r`nSource commit: $gitCommit ($gitState)`r`n"

$hashLines = Get-ChildItem -LiteralPath $package -Recurse -File | Sort-Object FullName | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
    $relative = $_.FullName.Substring($package.Length + 1).Replace('\','/')
    "$hash  $relative"
}
Set-Content -LiteralPath (Join-Path $package 'SHA256SUMS.txt') -Value $hashLines

Compress-Archive -LiteralPath $package -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
Write-Host "Package: $package"
Write-Host "ZIP:     $zipPath"
Write-Host "SHA256:  $zipHash"

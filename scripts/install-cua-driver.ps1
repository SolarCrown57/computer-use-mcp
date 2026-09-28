[CmdletBinding()]
param(
    [string]$LockFile,
    [string]$InstallRoot,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($LockFile)) {
    $LockFile = Join-Path $PSScriptRoot "..\cua-driver.lock.json"
}
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = Join-Path $PSScriptRoot "..\.tools\cua-driver"
}

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-Property {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object.PSObject.Properties[$Name]) {
        throw "Driver lock file is missing required property '$Name'."
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "This installer currently supports Windows only."
}

$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
if ($architecture -ne [System.Runtime.InteropServices.Architecture]::X64) {
    throw "Unsupported Windows architecture '$architecture'. The lock file only pins windows-x86_64."
}

$lockPath = Get-FullPath $LockFile
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Driver lock file not found: $lockPath"
}

$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($property in @("schema_version", "published", "repository", "tag", "version", "protocol", "assets")) {
    Assert-Property -Object $lock -Name $property
}

if ([int]$lock.schema_version -ne 1) {
    throw "Unsupported driver lock schema '$($lock.schema_version)'. Expected schema 1."
}
if ($lock.published -ne $true) {
    throw "Pinned driver $($lock.tag) is not published. Update published and sha256 only after the release asset passes E2E."
}
if ([string]$lock.protocol -ne "sc.background.v1") {
    throw "Unsupported driver protocol '$($lock.protocol)'. Expected sc.background.v1."
}

$assetProperty = $lock.assets.PSObject.Properties["windows-x86_64"]
if ($null -eq $assetProperty) {
    throw "Driver lock file has no windows-x86_64 asset."
}
$asset = $assetProperty.Value
foreach ($property in @("name", "url", "sha256")) {
    Assert-Property -Object $asset -Name $property
}

$assetName = [string]$asset.name
if ([System.IO.Path]::GetFileName($assetName) -ne $assetName -or -not $assetName.EndsWith(".zip")) {
    throw "Invalid driver asset name '$assetName'."
}

$expectedUrl = "https://github.com/$($lock.repository)/releases/download/$($lock.tag)/$assetName"
if ([string]$asset.url -ne $expectedUrl) {
    throw "Driver asset URL does not match the locked repository and tag. Expected: $expectedUrl"
}

$expectedSha256 = ([string]$asset.sha256).ToLowerInvariant()
if ($expectedSha256 -notmatch "^[0-9a-f]{64}$") {
    throw "Driver asset sha256 must contain exactly 64 hexadecimal characters."
}

$installRootPath = Get-FullPath $InstallRoot
$destination = Get-FullPath (Join-Path $installRootPath ([string]$lock.version))
$rootPrefix = $installRootPath.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $destination.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to install outside the configured install root."
}

if (Test-Path -LiteralPath $destination) {
    $existingDriver = @(Get-ChildItem -LiteralPath $destination -Filter "cua-driver.exe" -File -Recurse -ErrorAction SilentlyContinue)
    $existingManifestPath = Join-Path $destination "install-manifest.json"
    if (-not $Force -and $existingDriver.Count -eq 1 -and (Test-Path -LiteralPath $existingManifestPath -PathType Leaf)) {
        $existingManifest = Get-Content -LiteralPath $existingManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($existingManifest.archive_sha256 -eq $expectedSha256 -and $existingManifest.protocol -eq $lock.protocol) {
            [pscustomobject]@{
                command = $existingDriver[0].FullName
                version = [string]$lock.version
                protocol = [string]$lock.protocol
                reused = $true
            }
            return
        }
    }
    if (-not $Force) {
        throw "An unverified installation already exists at '$destination'. Re-run with -Force to replace it."
    }
    Remove-Item -LiteralPath $destination -Recurse -Force
}

New-Item -ItemType Directory -Path $installRootPath -Force | Out-Null
$staging = Join-Path $installRootPath (".install-" + [guid]::NewGuid().ToString("N"))
$archive = Join-Path $staging $assetName
$expanded = Join-Path $staging "expanded"

try {
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
    $oldProgressPreference = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri ([string]$asset.url) -OutFile $archive -UseBasicParsing
    }
    finally {
        $ProgressPreference = $oldProgressPreference
    }

    $actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $expectedSha256) {
        throw "Driver archive checksum mismatch. Expected $expectedSha256, received $actualSha256."
    }

    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
    $drivers = @(Get-ChildItem -LiteralPath $expanded -Filter "cua-driver.exe" -File -Recurse)
    if ($drivers.Count -ne 1) {
        throw "Expected exactly one cua-driver.exe in the release archive; found $($drivers.Count)."
    }

    $expandedPrefix = $expanded.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $drivers[0].FullName.StartsWith($expandedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Driver executable resolved outside the expanded archive."
    }
    $driverRelativePath = $drivers[0].FullName.Substring($expandedPrefix.Length)
    Move-Item -LiteralPath $expanded -Destination $destination
    $driverPath = Join-Path $destination $driverRelativePath
    $manifest = [ordered]@{
        schema_version = 1
        version = [string]$lock.version
        protocol = [string]$lock.protocol
        repository = [string]$lock.repository
        tag = [string]$lock.tag
        asset = $assetName
        archive_sha256 = $actualSha256
        command = $driverRelativePath
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination "install-manifest.json") -Encoding UTF8

    [pscustomobject]@{
        command = $driverPath
        version = [string]$lock.version
        protocol = [string]$lock.protocol
        reused = $false
    }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}

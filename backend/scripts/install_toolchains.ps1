<#
Installs the scaffolding toolchains DevFoundry can run (see app/services/scaffold_catalog.py) for the CURRENT USER:
no administrator rights, no installers, no UAC prompts. Every tool is an official portable archive extracted to
%LOCALAPPDATA%\DevFoundry\toolchains, and its folder is added to the user PATH (HKCU) - nothing machine-wide changes.

Usage (Windows PowerShell 5.1 or PowerShell 7):
    powershell -ExecutionPolicy Bypass -File backend\scripts\install_toolchains.ps1              # everything
    powershell -ExecutionPolicy Bypass -File backend\scripts\install_toolchains.ps1 -Only go,rust # a subset
    ... -Force   re-installs tools that are already present (to move to the latest version)

Restart the DevFoundry backend (and your terminal) afterwards so it sees the new PATH.
Node.js (npx), uv (uvx) and the .NET SDK are prerequisites installed separately (see README).
#>
param(
    [string[]]$Only = @(),
    [string]$Root = (Join-Path $env:LOCALAPPDATA "DevFoundry\toolchains"),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"           # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Downloads = Join-Path $Root "_downloads"
New-Item -ItemType Directory -Force $Root, $Downloads | Out-Null

function Get-Json([string]$Url) { Invoke-RestMethod -UseBasicParsing -Uri $Url -Headers @{ "User-Agent" = "DevFoundry" } }

function Save-File([string]$Url, [string]$Name) {
    $target = Join-Path $Downloads $Name
    Write-Host "    downloading $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $target -Headers @{ "User-Agent" = "DevFoundry" }
    return $target
}

function Expand-Into([string]$Archive, [string]$Destination) {
    if (Test-Path $Destination) { Remove-Item -Recurse -Force $Destination }
    New-Item -ItemType Directory -Force $Destination | Out-Null
    & tar.exe -xf $Archive -C $Destination          # built into Windows 10+, much faster than Expand-Archive
    if ($LASTEXITCODE -ne 0) { throw "could not extract $Archive" }
    Remove-Item -Force $Archive
}

function Add-UserPath([string]$Dir) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($current) { $parts = $current.Split(";") | Where-Object { $_ } }
    if ($parts -notcontains $Dir) { [Environment]::SetEnvironmentVariable("Path", (($parts + $Dir) -join ";"), "User") }
    if (($env:Path.Split(";")) -notcontains $Dir) { $env:Path = "$env:Path;$Dir" }
}

function Set-UserVariable([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
    Set-Item -Path "env:$Name" -Value $Value
}

function Find-Dir([string]$Parent, [string]$Pattern) {
    $dir = Get-ChildItem -Directory $Parent -Filter $Pattern | Select-Object -First 1
    if (-not $dir) { throw "$Pattern not found under $Parent" }
    return $dir.FullName
}

# ------------------------------------------------------------------------------------------------ tools
$Tools = [ordered]@{
    bun = @{ Check = "bun.exe"; Install = {
        $dir = Join-Path $Root "bun"
        Expand-Into (Save-File "https://github.com/oven-sh/bun/releases/latest/download/bun-windows-x64.zip" "bun.zip") $dir
        Add-UserPath (Find-Dir $dir "bun-windows-x64")
    } }
    deno = @{ Check = "deno.exe"; Install = {
        $dir = Join-Path $Root "deno"
        Expand-Into (Save-File "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" "deno.zip") $dir
        Add-UserPath $dir
    } }
    go = @{ Check = "go.exe"; Install = {
        $version = (Get-Json "https://go.dev/dl/?mode=json")[0].version
        $dir = Join-Path $Root "go"
        Expand-Into (Save-File "https://go.dev/dl/$version.windows-amd64.zip" "go.zip") $dir
        Add-UserPath (Join-Path $dir "go\bin")
    } }
    rust = @{ Check = "cargo.exe"; Install = {
        # GNU host: self-contained (no Visual Studio Build Tools, which would need an installer / admin)
        Set-UserVariable "RUSTUP_HOME" (Join-Path $Root "rust\rustup")
        Set-UserVariable "CARGO_HOME" (Join-Path $Root "rust\cargo")
        $init = Save-File "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe" "rustup-init.exe"
        & $init -y --no-modify-path --profile minimal --default-host x86_64-pc-windows-gnu --default-toolchain stable | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "rustup-init failed" }
        Remove-Item -Force $init
        Add-UserPath (Join-Path $Root "rust\cargo\bin")
    } }
    dart = @{ Check = "dart.exe"; Install = {
        $dir = Join-Path $Root "dart"
        Expand-Into (Save-File "https://storage.googleapis.com/dart-archive/channels/stable/release/latest/sdk/dartsdk-windows-x64-release.zip" "dart.zip") $dir
        Add-UserPath (Join-Path $dir "dart-sdk\bin")
        & (Join-Path $dir "dart-sdk\bin\dart.exe") --disable-analytics | Out-Null
    } }
    flutter = @{ Check = "flutter.bat"; Install = {
        $index = Get-Json "https://storage.googleapis.com/flutter_infra_release/releases/releases_windows.json"
        $release = $index.releases | Where-Object { $_.hash -eq $index.current_release.stable } | Select-Object -First 1
        $dir = Join-Path $Root "flutter"
        Expand-Into (Save-File "$($index.base_url)/$($release.archive)" "flutter.zip") $dir
        $bin = Join-Path $dir "flutter\bin"
        Add-UserPath $bin
        & (Join-Path $bin "flutter.bat") --disable-analytics | Out-Null
        & (Join-Path $bin "flutter.bat") --version | Out-Host
    } }
    java = @{ Check = "java.exe"; Install = {
        # Microsoft Build of OpenJDK, latest LTS; Maven and Gradle below need it
        $dir = Join-Path $Root "java"
        Expand-Into (Save-File "https://aka.ms/download-jdk/microsoft-jdk-25-windows-x64.zip" "jdk.zip") $dir
        $jdk = Find-Dir $dir "jdk-*"
        Set-UserVariable "JAVA_HOME" $jdk
        Add-UserPath (Join-Path $jdk "bin")
    } }
    maven = @{ Check = "mvn.cmd"; Install = {
        $meta = [xml](Invoke-WebRequest -UseBasicParsing "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/maven-metadata.xml").Content
        $version = $meta.metadata.versioning.versions.version | Where-Object { $_ -match '^\d+\.\d+\.\d+$' } |
            Sort-Object { [version]$_ } | Select-Object -Last 1    # latest stable (skips alpha / rc)
        $dir = Join-Path $Root "maven"
        Expand-Into (Save-File "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$version/apache-maven-$version-bin.zip" "maven.zip") $dir
        Add-UserPath (Join-Path (Find-Dir $dir "apache-maven-*") "bin")
    } }
    gradle = @{ Check = "gradle.bat"; Install = {
        $current = Get-Json "https://services.gradle.org/versions/current"
        $dir = Join-Path $Root "gradle"
        Expand-Into (Save-File $current.downloadUrl "gradle.zip") $dir
        Add-UserPath (Join-Path (Find-Dir $dir "gradle-*") "bin")
    } }
    php = @{ Check = "php.exe"; Install = {
        $releases = Get-Json "https://windows.php.net/downloads/releases/releases.json"
        $latest = $releases.PSObject.Properties | Sort-Object { [version]$_.Name } | Select-Object -Last 1
        $build = $latest.Value.PSObject.Properties | Where-Object { $_.Name -match '^nts-vs\d+-x64$' } | Select-Object -First 1
        $dir = Join-Path $Root "php"
        Expand-Into (Save-File "https://windows.php.net/downloads/releases/$($build.Value.zip.path)" "php.zip") $dir
        # php.ini with the extensions Composer and the PHP frameworks need
        $ini = Get-Content (Join-Path $dir "php.ini-production")
        $ini = $ini -replace '^;\s*extension_dir\s*=\s*"ext"', 'extension_dir = "ext"'
        foreach ($ext in "curl", "fileinfo", "intl", "mbstring", "openssl", "pdo_sqlite", "sqlite3", "zip") {
            $ini = $ini -replace "^;extension=$ext$", "extension=$ext"
        }
        Set-Content -Encoding ascii (Join-Path $dir "php.ini") $ini
        # Composer: the official phar plus a composer.bat shim next to php.exe
        $composer = Join-Path $dir "composer"
        New-Item -ItemType Directory -Force $composer | Out-Null
        Invoke-WebRequest -UseBasicParsing "https://getcomposer.org/download/latest-stable/composer.phar" -OutFile (Join-Path $composer "composer.phar")
        Set-Content -Encoding ascii (Join-Path $composer "composer.bat") "@php ""%~dp0composer.phar"" %*"
        Add-UserPath $dir
        Add-UserPath $composer
    } }
    elixir = @{ Check = "mix.bat"; Install = {
        $otp = Get-Json "https://api.github.com/repos/erlang/otp/releases/latest"
        $zip = $otp.assets | Where-Object { $_.name -match '^otp_win64_.*\.zip$' } | Select-Object -First 1
        $erlang = Join-Path $Root "erlang"
        Expand-Into (Save-File $zip.browser_download_url "otp.zip") $erlang
        Add-UserPath (Join-Path $erlang "bin")
        $major = ($otp.tag_name -replace '^OTP-', '').Split(".")[0]
        $ex = Get-Json "https://api.github.com/repos/elixir-lang/elixir/releases/latest"
        $exZip = $ex.assets | Where-Object { $_.name -eq "elixir-otp-$major.zip" } | Select-Object -First 1
        if (-not $exZip) {     # Elixir not yet built for this OTP: newest precompiled build (runs on newer OTP)
            $exZip = $ex.assets | Where-Object { $_.name -match '^elixir-otp-\d+\.zip$' } |
                Sort-Object { [int]($_.name -replace '\D', '') } | Select-Object -Last 1
        }
        $dir = Join-Path $Root "elixir"
        Expand-Into (Save-File $exZip.browser_download_url "elixir.zip") $dir
        Add-UserPath (Join-Path $dir "bin")
        & (Join-Path $dir "bin\mix.bat") local.hex --force | Out-Host
        & (Join-Path $dir "bin\mix.bat") archive.install hex phx_new --force | Out-Host
    } }
    pulumi = @{ Check = "pulumi.exe"; Install = {
        $version = (Invoke-WebRequest -UseBasicParsing "https://www.pulumi.com/latest-version").Content.Trim()
        $dir = Join-Path $Root "pulumi"
        Expand-Into (Save-File "https://get.pulumi.com/releases/sdk/pulumi-v$version-windows-x64.zip" "pulumi.zip") $dir
        Add-UserPath (Join-Path $dir "pulumi\bin")
    } }
}

# ------------------------------------------------------------------------------------------------ run
$Only = @($Only | ForEach-Object { $_.Split(",") } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
$unknown = $Only | Where-Object { -not $Tools.Contains($_) }
if ($unknown) { throw "Unknown tool(s): $($unknown -join ', '). Choose from: $($Tools.Keys -join ', ')" }
$selected = $Tools.Keys | Where-Object { $Only.Count -eq 0 -or $Only -contains $_ }
$results = [ordered]@{}
foreach ($name in $selected) {
    $tool = $Tools[$name]
    if (-not $Force -and (Get-Command $tool.Check -ErrorAction SilentlyContinue)) {
        Write-Host "[$name] already installed - skipped (use -Force to update)"
        $results[$name] = "already installed"
        continue
    }
    Write-Host "[$name] installing..."
    try {
        & $tool.Install
        $results[$name] = "installed"
        Write-Host "[$name] done"
    } catch {
        $results[$name] = "FAILED: $($_.Exception.Message)"
        Write-Warning "[$name] $($_.Exception.Message)"
    }
}
Remove-Item -Recurse -Force $Downloads -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Summary (install folder: $Root)"
foreach ($entry in $results.GetEnumerator()) { Write-Host ("  {0,-8} {1}" -f $entry.Key, $entry.Value) }
Write-Host "Restart the DevFoundry backend and your terminal so they pick up the new PATH."

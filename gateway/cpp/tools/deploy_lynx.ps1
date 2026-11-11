<#
.SYNOPSIS
    Deploy the C++ gateway's .wmopkg to the Westermo Lynx and (re)start it.

.DESCRIPTION
    Build the package first (in WSL):  gateway/cpp/dev.sh wmopkg
    Then, from the repository root on the laptop (Windows, PuTTY's plink on PATH):

        $env:LYNX_PASSWORD = "..."
        powershell -File gateway/cpp/tools/deploy_lynx.ps1

    -Autostart starts the app at boot; -Save makes the app definition
    survive a reboot (copy running-config startup-config, which saves
    the whole running config).

    Steps (gateway/cpp/LYNX.md has them by hand):
      1. serve gateway/cpp/dist/gateway-cpp-<version>.wmopkg over HTTP on the
         laptop's link-local address (the Lynx pulls; scp to it is off);
      2. `file app import` it on the Lynx;
      3. move the image label (gatewaycpp) to it;
      4. define/update the app (image, cmd, host network, /state share,
         GATEWAY_* environment);
      5. restart the app and wait for /health.
    Nothing else on the Lynx is changed; images of older versions stay.
    The Lynx allows 5 apps; the script stops if the app cannot be created.
    Test afterwards: python gateway/scripts/smoke_test.py --url http://<lynx>:8000/api/v1
#>
param(
    [string]$Lynx = "169.254.219.127",
    [string]$User = "admin",
    [string]$Password = $env:LYNX_PASSWORD,
    [string]$HostKey = "SHA256:EGKWPhMgqZMo7I6c4XrIsIBGimY75jOTXK92QX8FUz8",
    [string]$Version = "",
    [string]$App = "gateway",
    [string]$Label = "gatewaycpp",
    [string]$CameraIp = "169.254.1.223",
    [int]$Port = 8000,
    [int]$ServePort = 8099,
    [switch]$Autostart,
    [switch]$Save
)

$ErrorActionPreference = "Stop"

if (-not $Password) {
    throw "Give the Lynx password with -Password or `$env:LYNX_PASSWORD"
}

$repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$dist = Join-Path $repo "gateway\cpp\dist"

if (-not $Version) {
    $Version = (Select-String -Path (Join-Path $repo "gateway\cpp\CMakeLists.txt") -Pattern '^\s*VERSION ([0-9.]+)$').Matches[0].Groups[1].Value
}

$package = "gateway-cpp-$Version.wmopkg"

if (-not (Test-Path (Join-Path $dist $package))) {
    throw "$dist\$package missing: build it with gateway/cpp/dev.sh wmopkg $Version"
}

$ssh = @("-ssh", "-batch", "-hostkey", $HostKey, "-pw", $Password, "$User@$Lynx")

#
# plink writes "Using username" etc. to stderr, which Windows PowerShell
# turns into errors; judge by the exit code instead.
#
function Invoke-Lynx([string]$command, [switch]$AllowFailure) {
    # One EXEC-mode command
    $ErrorActionPreference = "Continue"
    & plink @ssh $command 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -notmatch "^Using username" }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw "Lynx command failed: $command" }
}

function Invoke-LynxSession([string[]]$lines) {
    # Several CLI lines (configuration mode), fed to an interactive session
    $ErrorActionPreference = "Continue"
    # The leading empty line takes the byte-order mark PowerShell may put
    # in front of piped text (else the first command is "not found").
    (("", $lines, "logout" | ForEach-Object { $_ }) -join "`n") + "`n" | & plink -T @ssh 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -notmatch "^Using username" }
}

# ----------------------------------------------------------------
# 1. Serve the package from the laptop's link-local address
# ----------------------------------------------------------------

#
# The local address Windows uses toward the Lynx (169.254.125.10 on
# the lab laptop's "Ethernet 2").
#
$laptop = (Find-NetRoute -RemoteIPAddress $Lynx -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress } |
    Select-Object -First 1).IPAddress

if (-not $laptop) { throw "No route to the Lynx at ${Lynx}: is the lab network connected?" }

#
# The repository's venv first: a bare `python` may be the Microsoft
# Store stub.
#
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }

Write-Host "Serving $package from http://${laptop}:$ServePort"

$server = Start-Process -FilePath $python -PassThru -WindowStyle Hidden `
    -ArgumentList "-m", "http.server", "$ServePort", "--bind", $laptop, "--directory", $dist

try {
    Start-Sleep -Seconds 2

    # ------------------------------------------------------------
    # 2. Import (replaces an image of the same name and version)
    # ------------------------------------------------------------

    #
    # Importing a version that is already there replaces it. (`file
    # app erase` cannot see images on external media on WeOS 5.30:
    # "Path does not exist"; old versions stay.)
    #
    Write-Host "Importing $package"
    Invoke-Lynx "file app import proto http ${laptop}:$ServePort /$package"
}
finally {
    Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
}

# ----------------------------------------------------------------
# 3. Label
# ----------------------------------------------------------------

Invoke-Lynx "app-hosting label gateway-cpp-$Version $Label"

# ----------------------------------------------------------------
# 4. App definition (setting a value again is harmless)
# ----------------------------------------------------------------

$config = @(
    "configure",
    "app-hosting",
    "app $App",
    "description `"Robot gateway (C++) $Version`"",
    "image $Label",
    "cmd `"/sbin/init --host 0.0.0.0 --port $Port`"",
    "netns host-os",
    "share media external app/$App/state as /state create",
    "environment GATEWAY_CAMERA_PROVIDER oak",
    "environment GATEWAY_CAMERA_IP $CameraIp",
    "environment GATEWAY_CALIBRATION_FILE /state/calibration.json",
    "enable",
    $(if ($Autostart) { "autostart" } else { "no autostart" }),
    "leave"
)

#
# Without saving, the app definition is lost at the next reboot. Saving
# writes the WHOLE running config, including anyone else's unsaved
# changes on this shared switch, hence opt-in.
#
if ($Save) {
    $config += "copy running-config startup-config"
}

Write-Host "Configuring app $App"
Invoke-LynxSession $config | Select-String -Pattern "rror|nvalid|ailed|not found|Max number" | ForEach-Object { Write-Warning $_ }

#
# The Lynx allows 5 apps; a sixth is refused ("Max number of apps
# reached" in `show log`).
#
$defined = Invoke-Lynx "app-hosting app $App" -AllowFailure
if ($LASTEXITCODE -ne 0 -or ($defined -match "not found")) {
    throw "App $App was not created on the Lynx (5 apps at most: remove an unused one, see gateway/cpp/LYNX.md); details: plink ... `"show log`""
}

# ----------------------------------------------------------------
# 5. (Re)start and wait for /health
# ----------------------------------------------------------------

Invoke-Lynx "app-hosting stop $App" -AllowFailure | Out-Null
Invoke-Lynx "app-hosting start $App"

$health = "http://${Lynx}:$Port/api/v1/health"
Write-Host "Waiting for $health (the camera can take up to a minute)"

$deadline = (Get-Date).AddSeconds(120)
while ($true) {
    try {
        $reply = Invoke-RestMethod -Uri $health -TimeoutSec 3
        if ($reply.camera.connected) { break }
    } catch { }

    if ((Get-Date) -gt $deadline) {
        Write-Warning "No healthy gateway after 120 s; its log:"
        Invoke-Lynx "app-hosting log $App" -AllowFailure | Select-Object -Last 30
        exit 1
    }
    Start-Sleep -Seconds 3
}

$reply | ConvertTo-Json -Depth 4
Write-Host "Gateway $Version running on the Lynx: http://${Lynx}:$Port"

param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

$expectedRemote = 'https://github.com/VRforty6/reel-memory.git'
$remote = (git remote get-url origin).Trim()
if ($remote -ne $expectedRemote) { throw "Refusing release from unexpected remote: $remote" }
if ((git branch --show-current).Trim() -ne 'main') { throw 'Refusing release outside main branch.' }

$gradleFile = Join-Path $repo 'android\app\build.gradle.kts'
$gradle = Get-Content $gradleFile -Raw
$versionCode = [int]([regex]::Match($gradle, 'versionCode\s*=\s*(\d+)').Groups[1].Value)
$versionName = [regex]::Match($gradle, 'versionName\s*=\s*"([^"]+)"').Groups[1].Value
if ($versionCode -lt 1 -or [string]::IsNullOrWhiteSpace($versionName)) { throw 'Invalid Android version.' }

$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    $advertisedLine = Get-Content $envFile | Where-Object { $_ -match '^UPDATE_VERSION_CODE=' } | Select-Object -First 1
    if ($advertisedLine) {
        $advertisedCode = [int]$advertisedLine.Substring('UPDATE_VERSION_CODE='.Length).Trim()
        if ($versionCode -le $advertisedCode) {
            throw "Android versionCode $versionCode must be greater than currently advertised $advertisedCode."
        }
    }
}

$settings = Get-Content (Join-Path $repo 'android\app\src\main\java\dev\reelmemory\app\data\SettingsStore.kt') -Raw
if ($settings -notmatch [regex]::Escape('https://vrforty6.tail434ddf.ts.net')) { throw 'Physical-device Tailscale backend default is missing.' }
if ($settings -notmatch [regex]::Escape('http://10.0.2.2:8000')) { throw 'Emulator backend default is missing.' }

$room = Get-Content (Join-Path $repo 'android\app\src\main\java\dev\reelmemory\app\data\ShareDatabase.kt') -Raw
if ($room -notmatch 'version\s*=\s*6' -or $room -notmatch 'MIGRATION_5_6') { throw 'Room schema is not the expected v6 migration chain.' }

Push-Location (Join-Path $repo 'android')
try {
    if ($SkipTests) { .\gradlew.bat assembleDebug --no-daemon }
    else { .\gradlew.bat testDebugUnitTest assembleDebug --no-daemon }
    if ($LASTEXITCODE -ne 0) { throw "Gradle failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$roomImpl = Join-Path $repo 'android\app\build\generated\ksp\debug\java\dev\reelmemory\app\data\ShareDatabase_Impl.java'
if (!(Test-Path $roomImpl)) { throw 'Generated Room schema implementation is missing.' }
$roomGenerated = Get-Content $roomImpl -Raw
$roomIdentity = [regex]::Match($roomGenerated, "VALUES\(42, '([0-9a-f]{32})'\)").Groups[1].Value
$expectedRoomIdentity = 'c83dc675d8cfb668cc81c6bf1a007f47'
if ($roomIdentity -ne $expectedRoomIdentity) {
    throw "Room identity changed ($roomIdentity). Add an intentional migration and update the release baseline before shipping."
}

$apk = Join-Path $repo 'android\app\build\outputs\apk\debug\app-debug.apk'
if (!(Test-Path $apk)) { throw 'Debug APK was not produced.' }

$localProperties = Get-Content (Join-Path $repo 'android\local.properties') -Raw
$sdkDir = [regex]::Match($localProperties, '(?m)^sdk\.dir=(.+)$').Groups[1].Value.Trim().Replace('\:', ':').Replace('\\', '\')
$buildTools = Get-ChildItem (Join-Path $sdkDir 'build-tools') -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending |
    Select-Object -First 1
if (!$buildTools) { throw 'No stable Android build-tools installation found.' }
$aapt = Join-Path $buildTools.FullName 'aapt.exe'
$apksigner = Join-Path $buildTools.FullName 'apksigner.bat'
$badging = (& $aapt dump badging $apk | Select-Object -First 1)
if ($badging -notmatch "name='dev\.reelmemory\.app'" -or $badging -notmatch "versionCode='$versionCode'" -or $badging -notmatch "versionName='$([regex]::Escape($versionName))'") {
    throw "APK package/version mismatch: $badging"
}

$runtime = Join-Path $repo '.runtime\updates'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$dest = Join-Path $runtime "reel-memory-$versionName.apk"
Copy-Item $apk $dest -Force
$sha = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLowerInvariant()
$size = (Get-Item $dest).Length

$envFile = Join-Path $repo '.env'
$previousPath = $null
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^UPDATE_APK_PATH=' } | Select-Object -First 1
    if ($line) { $previousPath = $line.Substring('UPDATE_APK_PATH='.Length).Trim() }
}
if ($previousPath -and (Test-Path $previousPath) -and (Test-Path $apksigner)) {
    $oldCert = (& $apksigner verify --print-certs $previousPath 2>$null | Select-String 'Signer #1 certificate SHA-256 digest:' | Select-Object -First 1).ToString()
    $newCert = (& $apksigner verify --print-certs $dest 2>$null | Select-String 'Signer #1 certificate SHA-256 digest:' | Select-Object -First 1).ToString()
    if (!$oldCert -or !$newCert -or $oldCert -ne $newCert) { throw 'APK signing certificate changed; refusing update publication.' }
}

Write-Output "RELEASE_OK version=$versionName code=$versionCode bytes=$size sha256=$sha"
Write-Output "UPDATE_APK_PATH=$($dest.Replace('\\','/'))"
Write-Output "UPDATE_VERSION_CODE=$versionCode"
Write-Output "UPDATE_VERSION_NAME=$versionName"

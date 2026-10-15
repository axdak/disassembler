$ErrorActionPreference = 'Stop'

# Resolve the repository root once so the test remains independent of the caller's working directory.
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceLauncher = Join-Path $projectRoot '启动.cmd'

if (-not (Test-Path -LiteralPath $sourceLauncher -PathType Leaf)) {
    throw "Launcher does not exist: $sourceLauncher"
}

function Write-CommandFile {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string[]] $Lines
    )

    # ASCII is sufficient for fake commands and avoids code-page differences in cmd.exe.
    Set-Content -LiteralPath $Path -Value $Lines -Encoding Ascii
}

function Invoke-TestLauncher {
    param(
        [Parameter(Mandatory)]
        [string] $LauncherPath,

        [Parameter(Mandatory)]
        [string] $FakeBinPath,

        [Parameter(Mandatory)]
        [string] $LogPath
    )

    # Execute the real batch file with controlled node/npm commands, preserving System32 for cmd utilities.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = "/d /s /c `"`"$LauncherPath`"`""
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment['PATH'] = "$FakeBinPath;$env:SystemRoot\System32;$env:SystemRoot"
    $startInfo.Environment['LAUNCHER_TEST_LOG'] = $LogPath

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    [void] $process.Start()
    $process.StandardInput.Close()
    $standardOutput = $process.StandardOutput.ReadToEnd()
    $standardError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output = "$standardOutput$standardError"
    }
}

function Assert-Equal {
    param(
        [Parameter(Mandatory)]
        [object] $Actual,

        [Parameter(Mandatory)]
        [object] $Expected,

        [Parameter(Mandatory)]
        [string] $Message
    )

    if ($Actual -ne $Expected) {
        throw "$Message`nExpected: $Expected`nActual: $Actual"
    }
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "novel launcher test $([guid]::NewGuid().ToString('N'))"

try {
    $workspace = Join-Path $tempRoot 'workspace with spaces'
    $desktopDir = Join-Path $workspace 'desktop'
    $fakeBin = Join-Path $tempRoot 'fake-bin'
    $callLog = Join-Path $tempRoot 'npm-calls.log'
    New-Item -ItemType Directory -Path $desktopDir, $fakeBin -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $desktopDir 'package.json') -Value '{}' -Encoding Ascii
    Copy-Item -LiteralPath $sourceLauncher -Destination (Join-Path $workspace '启动.cmd')

    Write-CommandFile -Path (Join-Path $fakeBin 'node.cmd') -Lines @(
        '@echo off',
        'exit /b 0'
    )
    Write-CommandFile -Path (Join-Path $fakeBin 'npm.cmd') -Lines @(
        '@echo off',
        'echo %CD%^|%*>>"%LAUNCHER_TEST_LOG%"',
        'if "%*"=="install" goto installed',
        'if "%*"=="ci" goto installed',
        'exit /b 0',
        ':installed',
        'mkdir node_modules\.bin node_modules\electron-vite\bin node_modules\electron\dist >nul 2>&1',
        'type nul >node_modules\.bin\electron-vite.cmd',
        'type nul >node_modules\electron-vite\bin\electron-vite.js',
        'type nul >node_modules\electron\dist\electron.exe',
        'exit /b 0'
    )

    # A path containing spaces must work, and a missing node_modules directory must trigger installation first.
    $successResult = Invoke-TestLauncher `
        -LauncherPath (Join-Path $workspace '启动.cmd') `
        -FakeBinPath $fakeBin `
        -LogPath $callLog
    Assert-Equal -Actual $successResult.ExitCode -Expected 0 -Message 'Launcher should return the npm dev exit code.'
    $expectedCalls = @(
        "$desktopDir|install",
        "$desktopDir|run dev"
    )
    if (-not (Test-Path -LiteralPath $callLog -PathType Leaf)) {
        throw "Launcher did not invoke npm. Process output:`n$($successResult.Output)"
    }
    $actualCalls = @(Get-Content -LiteralPath $callLog)
    Assert-Equal -Actual ($actualCalls -join "`n") -Expected ($expectedCalls -join "`n") -Message 'Launcher should install missing dependencies before starting development mode.'

    # A leftover directory is not proof of a working install. Prefer the exact
    # lockfile when repairing a missing CLI shim or an incomplete Electron binary.
    Set-Content -LiteralPath (Join-Path $desktopDir 'package-lock.json') -Value '{}' -Encoding Ascii
    foreach ($missingFile in @('node_modules\.bin\electron-vite.cmd', 'node_modules\electron-vite\bin\electron-vite.js', 'node_modules\electron\dist\electron.exe')) {
        Remove-Item -LiteralPath (Join-Path $desktopDir $missingFile) -Force
        Remove-Item -LiteralPath $callLog -Force
        $repairResult = Invoke-TestLauncher `
            -LauncherPath (Join-Path $workspace '启动.cmd') `
            -FakeBinPath $fakeBin `
            -LogPath $callLog
        Assert-Equal -Actual $repairResult.ExitCode -Expected 0 -Message "Launcher should repair missing $missingFile."
        Assert-Equal -Actual ((Get-Content -LiteralPath $callLog) -join "`n") `
            -Expected (@("$desktopDir|ci", "$desktopDir|run dev") -join "`n") `
            -Message 'A partial installation must be restored from the lockfile before launching.'
    }

    # Healthy dependencies must not cause installation on every startup.
    Remove-Item -LiteralPath $callLog -Force
    $healthyResult = Invoke-TestLauncher -LauncherPath (Join-Path $workspace '启动.cmd') -FakeBinPath $fakeBin -LogPath $callLog
    Assert-Equal -Actual $healthyResult.ExitCode -Expected 0 -Message 'Healthy dependencies should launch normally.'
    Assert-Equal -Actual ((Get-Content -LiteralPath $callLog) -join "`n") -Expected "$desktopDir|run dev" -Message 'Healthy startup should not reinstall.'

    # A failed development process must be surfaced to callers instead of being reported as successful.
    Remove-Item -LiteralPath $callLog -Force
    Write-CommandFile -Path (Join-Path $fakeBin 'npm.cmd') -Lines @(
        '@echo off',
        'echo %CD%^|%*>>"%LAUNCHER_TEST_LOG%"',
        'if "%*"=="run dev" exit /b 17',
        'exit /b 0'
    )
    $failureResult = Invoke-TestLauncher `
        -LauncherPath (Join-Path $workspace '启动.cmd') `
        -FakeBinPath $fakeBin `
        -LogPath $callLog
    Assert-Equal -Actual $failureResult.ExitCode -Expected 17 -Message 'Launcher should preserve a failed npm dev exit code.'

    # A failed repair must retain npm's exit code and must never start the app.
    Remove-Item -LiteralPath (Join-Path $desktopDir 'node_modules\.bin\electron-vite.cmd') -Force
    Remove-Item -LiteralPath $callLog -Force
    Write-CommandFile -Path (Join-Path $fakeBin 'npm.cmd') -Lines @(
        '@echo off', 'echo %CD%^|%*>>"%LAUNCHER_TEST_LOG%"', 'exit /b 23'
    )
    $installFailure = Invoke-TestLauncher -LauncherPath (Join-Path $workspace '启动.cmd') -FakeBinPath $fakeBin -LogPath $callLog
    Assert-Equal -Actual $installFailure.ExitCode -Expected 23 -Message 'Dependency restoration must preserve npm errors.'
    Assert-Equal -Actual ((Get-Content -LiteralPath $callLog) -join "`n") -Expected "$desktopDir|ci" -Message 'A failed repair must not start dev.'

    # npm reporting success is insufficient when Electron's actual files are missing.
    Remove-Item -LiteralPath $callLog -Force
    Write-CommandFile -Path (Join-Path $fakeBin 'npm.cmd') -Lines @(
        '@echo off', 'echo %CD%^|%*>>"%LAUNCHER_TEST_LOG%"', 'exit /b 0'
    )
    $incompleteResult = Invoke-TestLauncher -LauncherPath (Join-Path $workspace '启动.cmd') -FakeBinPath $fakeBin -LogPath $callLog
    Assert-Equal -Actual $incompleteResult.ExitCode -Expected 1 -Message 'Incomplete dependencies must fail after installation.'
    Assert-Equal -Actual ((Get-Content -LiteralPath $callLog) -join "`n") -Expected "$desktopDir|ci" -Message 'Do not launch or loop after an incomplete repair.'

    # Missing Node.js must stop before npm is invoked.
    Remove-Item -LiteralPath (Join-Path $fakeBin 'node.cmd') -Force
    Remove-Item -LiteralPath $callLog -Force
    $missingNodeResult = Invoke-TestLauncher `
        -LauncherPath (Join-Path $workspace '启动.cmd') `
        -FakeBinPath $fakeBin `
        -LogPath $callLog
    if ($missingNodeResult.ExitCode -eq 0) {
        throw 'Launcher should fail when Node.js is unavailable.'
    }
    if (Test-Path -LiteralPath $callLog) {
        throw 'Launcher should not invoke npm when Node.js is unavailable.'
    }

    Write-Output 'PASS: 启动.cmd launcher behavior'
}
finally {
    # Test artifacts are isolated under a unique temporary directory and can be removed safely.
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        $expectedPrefix = Join-Path ([System.IO.Path]::GetTempPath()) 'novel launcher test '
        if (-not $resolvedTempRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean unexpected test directory: $resolvedTempRoot"
        }
        Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
    }
}

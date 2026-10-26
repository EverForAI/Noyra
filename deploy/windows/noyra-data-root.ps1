# Child-only launch configuration and retained data-root admission. No state migration.
Set-StrictMode -Version Latest

function Assert-NoyraDirectoryChain([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($null -ne $current) {
        try {
            $attributes = [IO.File]::GetAttributes($current)
            if (-not ($attributes -band [IO.FileAttributes]::Directory) -or
                ($attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "NOYRA-DATA-ROOT[unsafe-path]: directory path contains a file or link: $current"
            }
        }
        catch [IO.FileNotFoundException] {}
        catch [IO.DirectoryNotFoundException] {}
        $parent = [IO.Directory]::GetParent($current)
        $current = if ($null -ne $parent) { $parent.FullName } else { $null }
    }
}

function Assert-NoyraRootSeparation {
    $comparison = [StringComparison]::OrdinalIgnoreCase
    foreach ($path in @($InstallRoot, $DataRoot)) {
        if ($path -eq [IO.Path]::GetPathRoot($path).TrimEnd([IO.Path]::DirectorySeparatorChar)) {
            throw 'NOYRA-DATA-ROOT[unsafe-root]: installation and data roots must be non-root directories.'
        }
        Assert-NoyraDirectoryChain $path
    }
    if ($DataRoot.Equals($InstallRoot, $comparison) -or
        $DataRoot.StartsWith($InstallRoot + [IO.Path]::DirectorySeparatorChar, $comparison) -or
        $InstallRoot.StartsWith($DataRoot + [IO.Path]::DirectorySeparatorChar, $comparison)) {
        throw 'NOYRA-DATA-ROOT[overlapping-roots]: installation and retained data roots must be disjoint.'
    }
    Assert-NoyraDirectoryChain (Join-Path $InstallRoot 'versions')
}

function Assert-NoyraStartDataRoot([bool]$Initialize = $false) {
    Assert-NoyraRootSeparation
    $database = Join-Path $DataRoot 'noyra.sqlite3'
    if (Test-Path -LiteralPath $database) {
        $item = Get-Item -LiteralPath $database -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -eq 0) {
            throw 'NOYRA-DATA-ROOT[invalid-database]: an existing database is empty, a directory or a link.'
        }
        if ($Initialize) { throw 'NOYRA-DATA-ROOT[existing-database]: InitializeDataRoot cannot select an existing database.' }
        return
    }
    if (-not $Initialize) {
        throw 'NOYRA-DATA-ROOT[database-missing]: locate and restore existing data first; only a deliberately new subject may use start -InitializeDataRoot.'
    }
    if (Test-Path -LiteralPath $DataRoot) {
        foreach ($item in @(Get-ChildItem -LiteralPath $DataRoot -Force)) {
            if ($item.Name -notin @('.noyra-lifecycle.lock', 'noyra.pid') -or $item.PSIsContainer -or
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'NOYRA-DATA-ROOT[initialization-not-empty]: initialization requires a directory without subject data.'
            }
        }
    }
}

function New-NoyraProcessStartInfo([string]$Launcher, [string]$WorkingDirectory, [bool]$Initialize = $false) {
    Assert-NoyraStartDataRoot $Initialize
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Launcher
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    # ProcessStartInfo owns its environment copy. Never mutate $env: in the parent.
    $info.EnvironmentVariables['NOYRA_DATA_DIR'] = $DataRoot
    $info.EnvironmentVariables['NOYRA_MANAGED_INSTALL_ROOT'] = $InstallRoot
    $info.EnvironmentVariables['NOYRA_MANAGED_DATA_ROOT'] = $DataRoot
    $info.EnvironmentVariables['NOYRA_MANAGED_INITIALIZE'] = if ($Initialize) { '1' } else { '0' }
    return $info
}

function Start-NoyraBoundProcess([string]$Launcher, [string]$WorkingDirectory, [bool]$Initialize = $false) {
    $info = New-NoyraProcessStartInfo $Launcher $WorkingDirectory $Initialize
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Noyra launcher did not start.' }
        return $process
    }
    catch { $process.Dispose(); throw }
}

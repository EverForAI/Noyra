# Windows package gate

The Windows package builder is a release gate. It must run in a complete
PowerShell 7 runtime on a Windows runner; a partial `pwsh` bundle is not an
acceptable substitute because package assembly and hash verification depend on
the standard filesystem cmdlets.

`scripts/build-windows-package.ps1` performs a preflight before it resolves the
source directory or creates a staging directory. It verifies that
`Microsoft.PowerShell.Management` is discoverable and loadable, then checks
that `Resolve-Path`, `Test-Path`, `Copy-Item`, and `Get-FileHash` are available
as cmdlets. A failed preflight exits with status `78` and a diagnostic whose
prefix is:

```text
NOYRA-WINDOWS-PACKAGE-PREREQUISITE[...]:
```

The diagnostic identifies either the missing module (`PS-MANAGEMENT-MODULE`)
or cmdlet (`PS-CMDLET`) and tells the operator to use a complete PowerShell 7
runtime. This prevents a missing module from being reported as a package input,
hash, or signing failure.

The supported CI gate is the `windows-2022` matrix job in
`.github/workflows/ci.yml`. It must run both package lifecycle tests and the
hash-tampering rejection test. A local or embedded runtime that cannot load
the Management module remains an external gate failure; it must not be
converted into a passing release result.

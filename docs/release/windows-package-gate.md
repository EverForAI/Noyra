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


## Launcher and publisher contract

A package build is not complete without an explicit `-LauncherPath` pointing to
the signed Noyra launcher executable. The builder writes the launcher as the
manifest-declared `noyra.exe` entry; it no longer silently creates a source-only
package that the lifecycle can install but never start. For strict signing, pass
one or more approved publisher certificate thumbprints with
`-TrustedPublisherThumbprints` and `-RequireSignature`. The lifecycle receives
the same approved thumbprints and checks the manifest-declared launcher and every
packaged executable against that publisher allowlist. Authenticode `Valid` alone
is not an Noyra release identity.

The thumbprint allowlist is an operator/release policy input and must be
reviewed and rotated with the publisher certificate. The package manifest is
not itself a publisher signature; the release pipeline must still sign and
verify the release checksum/manifest bundle and retain the clean-VM evidence
required by the external release gate.

param(
    [Parameter(Mandatory=$true)][string]$VerifiedRollbackProfile,
    [Parameter(Mandatory=$true)][string]$ApprovedPlan,
    [Parameter(Mandatory=$true)][ValidateSet('scanner','listener')][string]$Role,
    [Parameter(Mandatory=$true)][string]$PacketRoot,
    [string]$ReleaseContract
)
$ErrorActionPreference = 'Stop'
# A newly staged exact transport/state compatibility proof is required. Historical
# rollback releases are not presumed rich-compatible. Never change Git or SQLite.
& (Join-Path $PSScriptRoot 'start_runtime.ps1') -Role $Role -Release rollback `
  -ProposedProfile $VerifiedRollbackProfile -ApprovedPlan $ApprovedPlan -PacketRoot $PacketRoot -ReleaseContract $ReleaseContract
if ($LASTEXITCODE -ne 0) { throw 'Compatible rollback launch failed; preserve current state and inspect' }

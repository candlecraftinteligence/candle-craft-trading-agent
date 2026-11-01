param(
    [Parameter(Mandatory=$true)][ValidateSet('scanner','listener')][string]$Role,
    [Parameter(Mandatory=$true)][ValidateSet('repair','rollback')][string]$Release,
    [Parameter(Mandatory=$true)][string]$ProposedProfile,
    [Parameter(Mandatory=$true)][string]$ApprovedPlan,
    [Parameter(Mandatory=$true)][string]$PacketRoot
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-G7M42AL') { throw 'Runtime-only; no DEV scanner/listener operation' }
$RuntimeRepo = 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
$PythonPath = Join-Path $RuntimeRepo '.venv\Scripts\python.exe'
$ProfileTool = Join-Path $PacketRoot 'scripts\cci_v2_release_profiles.py'
# Tool verifies the original profile hash, complete epoch/args, code identity and
# exclusive role ownership. It creates a hidden dedicated console and never opens DB.
& $PythonPath $ProfileTool start $Role --release $Release --output $ProposedProfile --approval $ApprovedPlan
if ($LASTEXITCODE -ne 0) { throw 'Launch refused or failed; inspect owners before retrying' }

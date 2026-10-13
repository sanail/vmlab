# Grant a Lab's permissions ([labs.<name>] grant) to apps in a Windows Guest, or check that they hold.
#
# vmlab sends this script with every call, as it does vmlab-spawn.ps1: stdin is one line of JSON
# parameters, then this script, run as a script block with the command name and that line.
#
#   apply {"grants": [[name, app], ...]}   writes the grants
#   check {"grants": [[name, app], ...]}   prints one line per grant that does not hold; exit 1 if any
#
# Only camera and microphone ask: the user's ConsentStore holds an "Allow" for the app, a desktop
# app's exe path with \ written as # under NonPackaged, a packaged app's family name directly; the
# switches above it ("let apps", "let desktop apps") are turned on too. The other names ask nothing.
# Plain ASCII on purpose: Windows PowerShell reads a script in the system code page.
param([string]$Command, [string]$Params)
$ErrorActionPreference = 'Stop'
$store = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore'
$capabilities = @{ camera = 'webcam'; microphone = 'microphone' }
$missing = 0
foreach ($grant in (ConvertFrom-Json $Params).grants) {
    $capability = $capabilities[$grant[0]]
    if (-not $capability) { continue }
    $app = $grant[1]
    $keys = @("$store\$capability")
    if ($app -match '[\\/]') { $keys += "$store\$capability\NonPackaged", ("$store\$capability\NonPackaged\" + ($app -replace '[\\/]', '#')) }
    else { $keys += "$store\$capability\$app" }
    foreach ($key in $keys) {
        if ($Command -eq 'apply') {
            if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
            Set-ItemProperty -LiteralPath $key -Name Value -Value Allow -Type String
        } elseif ((Get-ItemProperty -LiteralPath $key -Name Value -ErrorAction SilentlyContinue).Value -ne 'Allow') {
            Write-Output "$($grant[0]) is not allowed for $app ($key)"
            $missing = 1
        }
    }
}
exit $missing

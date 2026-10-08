# Load deploy/aws/settings.env into Parameter Store under /pickup-booking/ (run from the repo root):
#   powershell -File deploy/aws/put-settings.ps1
# Secrets are stored encrypted (SecureString); a blank value is skipped, so a secret already in
# Parameter Store stays as it is. Restart the board afterwards to apply a change.
param(
    [string]$File = "deploy/aws/settings.env",
    [string]$Profile = "paybot-admin",
    [string]$Region = "us-east-1",
    [string]$Path = "/pickup-booking/"
)
$ErrorActionPreference = "Stop"
$secrets = @("TPRO_USERNAME", "TPRO_PASSWORD", "FP_GOOGLE_CLIENT_SECRET", "FP_BOARD_SESSION_SECRET")

if (-not (Test-Path $File)) {
    throw "No ${File}: copy deploy/aws/settings.env.example to it and fill it in."
}
foreach ($line in Get-Content $File) {
    $text = $line.Trim()
    if ($text -eq "" -or $text.StartsWith("#")) { continue }
    $name, $value = $text -split "=", 2
    $name = $name.Trim()
    $value = "$value".Trim()
    if ($value -eq "") {
        Write-Host "skipped $name (blank)"
        continue
    }
    $type = if ($secrets -contains $name) { "SecureString" } else { "String" }
    aws ssm put-parameter --profile $Profile --region $Region --name "$Path$name" --value $value --type $type --overwrite --output text | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not store $name" }
    Write-Host "stored $name ($type)"
}

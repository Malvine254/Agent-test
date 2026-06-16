# Script to create a new bot client secret
# Run this script to generate a new password for your bot

$AppId = "8077e820-3063-4981-9fb6-b281b28c854b"
$DisplayName = "Bot Secret for Local Development"

Write-Host "Creating new client secret for bot app: $AppId" -ForegroundColor Cyan
Write-Host ""

# Check if logged in to Azure
$account = az account show 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Not logged in to Azure. Logging in..." -ForegroundColor Yellow
    az login
}

# Create new client secret
Write-Host "Creating new client secret..." -ForegroundColor Yellow
$secretJson = az ad app credential reset --id $AppId --append --display-name $DisplayName --years 2 2>&1

if ($LASTEXITCODE -eq 0) {
    $secret = $secretJson | ConvertFrom-Json
    $password = $secret.password
    
    Write-Host ""
    Write-Host "SUCCESS! New client secret created:" -ForegroundColor Green
    Write-Host ""
    Write-Host "Bot Password (copy this value):" -ForegroundColor Yellow
    Write-Host $password -ForegroundColor White
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "1. Copy the password above"
    Write-Host "2. Update env/.env.local.user with: SECRET_BOT_PASSWORD=$password"
    Write-Host "3. Update env/.env.dev.user with: SECRET_BOT_PASSWORD=$password"
    Write-Host "4. Restart your bot application"
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "ERROR: Failed to create client secret" -ForegroundColor Red
    Write-Host $secretJson
    Write-Host ""
    Write-Host "Manual steps:" -ForegroundColor Yellow
    Write-Host "1. Go to Azure Portal: https://portal.azure.com"
    Write-Host "2. Navigate to: Azure Active Directory > App registrations"
    Write-Host "3. Search for app ID: $AppId"
    Write-Host "4. Go to: Certificates & secrets > Client secrets > New client secret"
    Write-Host "5. Copy the secret value and update your .env files"
}

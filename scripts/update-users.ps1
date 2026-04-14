param(
    [string]$CsvPath = "users.csv"
)

# Bulk-populates Entra ID user profile fields from a CSV.
# CSV columns: UserPrincipalName, JobTitle, BusinessPhone, MobilePhone, Department, OfficeLocation,
#              StreetAddress, City, State, PostalCode, Country, CompanyName

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Users)) {
    Write-Host "Installing Microsoft.Graph.Users module..." -ForegroundColor Yellow
    Install-Module -Name Microsoft.Graph.Users -Scope CurrentUser -Force
}

Connect-MgGraph -Scopes "User.ReadWrite.All"

$users = Import-Csv -Path $CsvPath
$ok = 0; $fail = 0

foreach ($u in $users) {
    $params = @{ UserId = $u.UserPrincipalName }
    if ($u.JobTitle)       { $params.JobTitle       = $u.JobTitle }
    if ($u.Department)     { $params.Department     = $u.Department }
    if ($u.OfficeLocation) { $params.OfficeLocation = $u.OfficeLocation }
    if ($u.BusinessPhone)  { $params.BusinessPhones = @($u.BusinessPhone) }
    if ($u.MobilePhone)    { $params.MobilePhone    = $u.MobilePhone }
    if ($u.StreetAddress)  { $params.StreetAddress  = $u.StreetAddress }
    if ($u.City)           { $params.City           = $u.City }
    if ($u.State)          { $params.State          = $u.State }
    if ($u.PostalCode)     { $params.PostalCode     = $u.PostalCode }
    if ($u.Country)        { $params.Country        = $u.Country }
    if ($u.CompanyName)    { $params.CompanyName    = $u.CompanyName }

    try {
        Update-MgUser @params
        Write-Host "OK   $($u.UserPrincipalName)" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "FAIL $($u.UserPrincipalName) - $_" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host "Updated: $ok   Failed: $fail"
Disconnect-MgGraph | Out-Null
